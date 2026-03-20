# Requesty + Langfuse analytics (local)

## Prérequis

- Python 3.11+
- Fichier `.env` à la racine (voir `.env.example`)

## Lancer

```bash
pip install -r requirements.txt
python collector.py --auto --port 7842
```

Puis ouvrir [http://127.0.0.1:7842](http://127.0.0.1:7842) (clé API Requesty via `REQUESTY_KEY` dans `.env`).

## Tests des endpoints

```bash
./test_local.sh [token] [http://127.0.0.1:7842]
```

## Optionnel

- Enrichissement churn Mongo : variables `WONKA_MONGO_*` — détail des collections dans `MONGODB_STRUCTURE.md`.
