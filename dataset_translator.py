#!/usr/bin/env python3
"""
dataset_translator.py
======================

Traduit toutes les chaînes de caractères (littéraux entre guillemets) d'un
fichier texte/code source, avec cache SQLite persistant, traduction
parallèle et écriture atomique du résultat.

Conçu pour être générique : n'importe qui peut l'utiliser sur n'importe
quelle paire de langues, sans toucher au code — tout se règle via les
arguments de ligne de commande.

Exemple :
    python dataset_translator.py dataset.txt --source en --target fr

Limites connues (à garder en tête) :
    - L'extraction des chaînes se fait par regex, pas par un vrai parseur.
      Sur du code source réel, cela peut capturer des chaînes qui ne
      devraient pas être traduites (clés de dictionnaire, identifiants,
      code injecté dans des f-strings, etc.). Relire le résultat avant
      de l'utiliser en production.
    - Le module deep-translator s'appuie sur des services publics
      (Google Translate par défaut) : respecte leurs conditions
      d'utilisation et évite les volumes abusifs.

Auteur : amélioration du script original.
Licence : libre d'utilisation et de modification.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

try:
    from deep_translator import GoogleTranslator
except ImportError:
    print(
        "❌ Le module 'deep_translator' est requis.\n"
        "   Installe-le avec : pip install deep-translator",
        file=sys.stderr,
    )
    sys.exit(1)


# ============================================================
# CONFIGURATION (valeurs par défaut, toutes surchageables en CLI)
# ============================================================

@dataclass
class Config:
    input_path: str
    output_path: str
    cache_db: str = "translation_cache.db"
    source_lang: str = "en"
    target_lang: str = "fr"
    workers: int = 10
    max_retries: int = 5
    max_chars: int = 500
    retry_delay: float = 2.0
    dry_run: bool = False
    verbose: bool = False


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description="Traduit toutes les chaînes de caractères d'un fichier, avec cache et parallélisation.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", help="Fichier d'entrée à traduire")
    parser.add_argument(
        "-o", "--output",
        help="Fichier de sortie (par défaut : <input>_<target_lang>.<ext>)",
        default=None,
    )
    parser.add_argument("--cache-db", default="translation_cache.db",
                         help="Base SQLite servant de cache de traduction")
    parser.add_argument("--source", default="en", help="Langue source (code ISO)")
    parser.add_argument("--target", default="fr", help="Langue cible (code ISO)")
    parser.add_argument("--workers", type=int, default=10,
                         help="Nombre de traductions simultanées")
    parser.add_argument("--max-retries", type=int, default=5,
                         help="Nombre maximum de tentatives par chaîne")
    parser.add_argument("--max-chars", type=int, default=500,
                         help="Taille maximale d'un morceau envoyé au service de traduction")
    parser.add_argument("--retry-delay", type=float, default=2.0,
                         help="Délai de base (secondes) entre les tentatives (backoff linéaire)")
    parser.add_argument("--dry-run", action="store_true",
                         help="N'écrit rien : affiche seulement les statistiques d'extraction")
    parser.add_argument("-v", "--verbose", action="store_true",
                         help="Active les logs détaillés (niveau DEBUG)")

    args = parser.parse_args()

    output_path = args.output
    if output_path is None:
        base, ext = os.path.splitext(args.input)
        output_path = f"{base}_{args.target}{ext}"

    return Config(
        input_path=args.input,
        output_path=output_path,
        cache_db=args.cache_db,
        source_lang=args.source,
        target_lang=args.target,
        workers=args.workers,
        max_retries=args.max_retries,
        max_chars=args.max_chars,
        retry_delay=args.retry_delay,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )


# ============================================================
# LOGGING
# ============================================================

def setup_logging(verbose: bool) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger("dataset_translator")


# ============================================================
# CACHE SQLITE (thread-safe)
# ============================================================

class TranslationCache:
    """Cache persistant clé -> traduction, sauvegardé au fil de l'eau."""

    def __init__(self, db_path: str):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS translations (
                original TEXT PRIMARY KEY,
                translated TEXT NOT NULL
            )
            """
        )
        self.db.commit()
        self.lock = threading.Lock()
        self._memory: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        cursor = self.db.execute("SELECT original, translated FROM translations")
        for original, translated in cursor.fetchall():
            self._memory[original] = translated

    def __len__(self) -> int:
        return len(self._memory)

    def __contains__(self, key: str) -> bool:
        return key in self._memory

    def get(self, key: str) -> Optional[str]:
        return self._memory.get(key)

    def set(self, key: str, value: str) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO translations (original, translated) VALUES (?, ?)",
                (key, value),
            )
            self.db.commit()
            self._memory[key] = value

    def close(self) -> None:
        self.db.close()


# ============================================================
# DÉCOUPAGE DE TEXTE LONG
# ============================================================

def split_text(text: str, max_chars: int) -> list[str]:
    """Découpe un texte en morceaux <= max_chars, en respectant les lignes si possible."""
    if len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    current = ""

    for paragraph in text.split("\n"):
        if not current:
            current = paragraph
        elif len(current) + len(paragraph) + 1 <= max_chars:
            current += "\n" + paragraph
        else:
            parts.append(current)
            if len(paragraph) <= max_chars:
                current = paragraph
            else:
                for i in range(0, len(paragraph), max_chars):
                    parts.append(paragraph[i:i + max_chars])
                current = ""

    if current:
        parts.append(current)

    return parts


# ============================================================
# TRADUCTEUR
# ============================================================

class Translator:
    """Encapsule les appels au service de traduction, avec retry et cache."""

    def __init__(self, cache: TranslationCache, config: Config, logger: logging.Logger):
        self.cache = cache
        self.config = config
        self.logger = logger

    def translate(self, text: str) -> tuple[str, bool]:
        """Retourne (texte_traduit, était_en_cache)."""
        cached = self.cache.get(text)
        if cached is not None:
            return cached, True

        if not text.strip():
            return text, False

        parts = split_text(text, self.config.max_chars)
        translated_parts = [self._translate_part(part) for part in parts]
        final = "\n".join(translated_parts)

        self.cache.set(text, final)
        return final, False

    def _translate_part(self, part: str) -> str:
        for attempt in range(1, self.config.max_retries + 1):
            try:
                translator = GoogleTranslator(
                    source=self.config.source_lang,
                    target=self.config.target_lang,
                )
                result = translator.translate(part)
                if not result:
                    raise ValueError("Réponse vide du service de traduction")
                return result.strip()
            except Exception as exc:
                self.logger.warning(
                    "Tentative %d/%d échouée : %s",
                    attempt, self.config.max_retries, str(exc)[:120],
                )
                if attempt < self.config.max_retries:
                    time.sleep(self.config.retry_delay * attempt)

        self.logger.error("Échec définitif pour un segment, texte original conservé.")
        return part


# ============================================================
# EXTRACTION DES CHAÎNES
# ============================================================

STRING_PATTERN = re.compile(r"""(['"])((?:\\.|(?!\1).)*?)\1""", re.DOTALL)


def extract_strings(content: str) -> list[re.Match]:
    return list(STRING_PATTERN.finditer(content))


# ============================================================
# ÉCRITURE ATOMIQUE DU FICHIER DE SORTIE
# ============================================================

def render_output(content: str, matches: list[re.Match], translations: dict[str, str]) -> str:
    output_parts = []
    last_position = 0

    for match in matches:
        output_parts.append(content[last_position:match.start()])

        quote = match.group(1)
        original = match.group(2)
        translated = translations.get(original, original)

        translated = translated.replace("\\", "\\\\")
        translated = translated.replace(quote, "\\" + quote)

        output_parts.append(quote + translated + quote)
        last_position = match.end()

    output_parts.append(content[last_position:])
    return "".join(output_parts)


def write_atomic(path: str, text: str) -> None:
    temp_file = path + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_file, path)


# ============================================================
# ORCHESTRATEUR PRINCIPAL
# ============================================================

def run(config: Config) -> None:
    logger = setup_logging(config.verbose)

    logger.info("Lecture de %s...", config.input_path)
    with open(config.input_path, "r", encoding="utf-8") as f:
        content = f.read()
    logger.info("Taille : %.2f Mo", len(content) / 1024 / 1024)

    matches = extract_strings(content)
    logger.info("Chaînes trouvées : %d", len(matches))

    cache = TranslationCache(config.cache_db)
    logger.info("Cache chargé : %d traductions existantes", len(cache))

    seen: set[str] = set()
    unique_texts: list[str] = []
    for match in matches:
        original = match.group(2)
        if original in seen:
            continue
        seen.add(original)
        if original not in cache:
            unique_texts.append(original)

    total_new = len(unique_texts)
    logger.info("Nouvelles traductions à faire : %d", total_new)
    logger.info("Déjà en cache : %d", len(seen) - total_new)

    if config.dry_run:
        logger.info("Mode dry-run : aucun fichier ne sera écrit.")
        cache.close()
        return

    translations: dict[str, str] = {t: cache.get(t) for t in seen if t in cache}

    output_lock = threading.Lock()

    def save_progress() -> None:
        with output_lock:
            rendered = render_output(content, matches, translations)
            write_atomic(config.output_path, rendered)

    save_progress()  # première version immédiate

    translator = Translator(cache, config, logger)
    start = time.time()
    completed = 0

    if total_new > 0:
        with ThreadPoolExecutor(max_workers=config.workers) as executor:
            futures = {executor.submit(translator.translate, text): text for text in unique_texts}

            try:
                for future in as_completed(futures):
                    original = futures[future]
                    try:
                        translated, _was_cached = future.result()
                        translations[original] = translated
                        completed += 1
                        save_progress()

                        percent = completed / total_new * 100
                        preview = translated[:100].replace("\n", " ")
                        logger.info("[%d/%d] %.1f%% | %s", completed, total_new, percent, preview)
                    except Exception as exc:
                        logger.error("Erreur sur une traduction : %s", str(exc)[:150])
            except KeyboardInterrupt:
                logger.warning("Interruption utilisateur — sauvegarde des traductions déjà faites.")
                save_progress()
                cache.close()
                raise

    save_progress()
    elapsed = time.time() - start
    cache.close()

    logger.info("=" * 60)
    logger.info("✅ TRADUCTION TERMINÉE")
    logger.info("=" * 60)
    logger.info("Chaînes trouvées      : %d", len(matches))
    logger.info("Nouvelles traductions : %d", total_new)
    logger.info("Cache                 : %s", config.cache_db)
    logger.info("Fichier de sortie     : %s", config.output_path)
    if os.path.exists(config.output_path):
        logger.info("Taille finale         : %.2f Mo",
                     os.path.getsize(config.output_path) / 1024 / 1024)
    logger.info("Temps                 : %.2f minutes", elapsed / 60)
    logger.info("=" * 60)


def main() -> None:
    config = parse_args()
    try:
        run(config)
    except FileNotFoundError:
        print(f"❌ Fichier introuvable : {config.input_path}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
