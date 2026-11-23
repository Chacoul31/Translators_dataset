# dataset_translator

Traduit toutes les chaînes de caractères d'un fichier (dataset, code, JSON,
texte...) avec cache persistant, traduction parallèle, et écriture atomique.

## Installation

```bash
pip install deep-translator
```

## Utilisation

```bash
# Cas simple : en -> fr, sortie automatique
python dataset_translator.py dataset.txt

# Choisir la langue source/cible et le fichier de sortie
python dataset_translator.py data.json --source en --target es -o data_es.json

# Voir juste les statistiques sans rien écrire
python dataset_translator.py dataset.txt --dry-run

# Ajuster la charge (plus de workers = plus rapide mais plus de requêtes)
python dataset_translator.py dataset.txt --workers 20 --max-retries 3
```

## Options principales

| Option | Rôle |
|---|---|
| `--source` / `--target` | Codes de langue ISO (ex: `en`, `fr`, `es`) |
| `--cache-db` | Fichier SQLite de cache (réutilisable entre exécutions) |
| `--workers` | Nombre de traductions en parallèle |
| `--max-retries` | Tentatives avant abandon d'un segment |
| `--max-chars` | Taille max envoyée au service par requête |
| `--dry-run` | Analyse sans écrire de fichier |
| `-v` / `--verbose` | Logs détaillés |

## Ce qui a changé par rapport à la version d'origine

- **Configurable en CLI** : plus besoin d'éditer le code pour changer de
  langue, de fichier ou de réglages — utile pour que d'autres personnes
  s'en servent sans toucher au script.
- **Logging structuré** au lieu de `print` bruts (niveaux INFO/WARNING/ERROR,
  horodatage, mode verbeux).
- **Code organisé en classes** (`TranslationCache`, `Translator`) plutôt
  qu'en variables globales — plus facile à tester et à réutiliser dans
  un autre projet.
- **Mode `--dry-run`** pour estimer le travail avant de lancer une vraie
  traduction (utile sur de gros datasets).
- **Gestion d'erreurs plus claire** : fichier introuvable, interruption
  clavier, échecs de traduction individuels n'interrompent plus tout le
  traitement.

## Limites à connaître

- L'extraction des chaînes se fait par expression régulière, pas par un
  vrai parseur de code. Sur du code source réel, certaines chaînes
  capturées ne devraient pas être traduites (clés de dictionnaire,
  identifiants). Relis toujours le résultat avant de l'utiliser.
- Le module `deep-translator` s'appuie par défaut sur Google Translate :
  reste raisonnable sur les volumes pour éviter les blocages, et vérifie
  que cet usage respecte les conditions du service.
