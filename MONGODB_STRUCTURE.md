# Structure MongoDB : userId → email + organisation

Ce document décrit comment les **userId** (Langfuse / Wonka) correspondent aux données utilisateur dans MongoDB pour récupérer **email** et **organisations**.

## Churn dashboard (`/analytics/churn-users`)

Les stats churn viennent de la collection **`messages`** (plus Langfuse) :

- Agrégation par champ **`user`** : `min(createdAt)` = 1er message, `max(createdAt)` = dernier, `count` = nombre de messages.
- **Email / organisations** : jointure sur **`users._id`** (même identifiant que `messages.user` en ObjectId hex).

Variables optionnelles (`.env`) :

| Variable | Effet |
| -------- | ----- |
| `WONKA_CHURN_MESSAGES_LOOKBACK_DAYS` | Si défini (ex. `90`), ne considère que les messages récents (sinon historique complet). |
| `WONKA_CHURN_USER_MESSAGES_ONLY=1` | Ne compte que les messages avec `isCreatedByUser: true`. |
| `WONKA_CHURN_MAX_TIME_MS` | Timeout agrégation (défaut 180000). |

Sans **`WONKA_MONGO_URI`**, le churn ne peut pas charger.

La réponse **`mongo_enrich`** inclut notamment `source: "mongodb_messages"`, `users_from_messages`, `profiles_matched` / `profiles_requested` pour l’enrichissement email/org.

## Bases disponibles

- **`wonkachat-prod`** — base par défaut (`WONKA_MONGO_DB` dans [collector.py](collector.py))
- **`wonkachat-dev`** — même schéma

## Lien userId

| Concept                        | Où c’est                                  |
| ------------------------------ | ----------------------------------------- |
| **userId** (churn / messages) | `messages.user` → même valeur que `users._id` (souvent string hex 24) |

Les requêtes doivent utiliser un ObjectId valide. Les clés sont comparées en **minuscules** côté enrichisseur pour éviter les écarts de casse.

## Collection `users` (wonkachat-prod)

Champs utiles pour l’enrichissement churn :

- **`email`** : string
- **`organizationMemberships`** : tableau d’objets avec notamment :
  - **`organizationId`** : ObjectId → clé vers `organizations._id`
  - `role`, `permissions`, `joinedAt`, etc.

Autres champs : `name`, `username`, `provider`, `role` (rôle global). Pour le churn enrichi, l’organisation métier vient du **$lookup** sur `organizations`.

## Collection `organizations`

- **`_id`** : ObjectId (référencé par `organizationMemberships.organizationId`)
- **`name`** : nom affiché de l’org (utilisé dans `wonka_user_profiles_by_ids` via `$map` sur `$$o.name`)
- Autres champs : `slug`, `subscription`, `billingInfo`, `superOrganizationId`, etc.

## Implémentation dans le projet

[collector.py](collector.py) : **`wonka_user_profiles_by_ids(user_ids)`** :

1. Convertit les `user_id` en ObjectIds valides
2. `$match` sur `users._id` dans la liste d’ObjectIds
3. **$lookup** `from: "organizations"` avec `localField: "organizationMemberships.organizationId"` et `foreignField: "_id"`
4. Projette `email` et la liste des noms d’organisations

Retour : `( { user_id_lower: { email, organizations } }, meta )`. Utilisé par **`/analytics/churn-users`**.

## Usage par organisation (`GET /analytics/usage-by-wonka-org?period=30d`)

Agrège les **messages** (`messages.createdAt` dans la fenêtre choisie) par utilisateur, puis répartit par **nom d’organisation** (profil `users` → `organizations.name`). Période alignée sur le sélecteur du dashboard (`1d` … `90d`). Multi-org : chaque message est compté en **parts égales** entre les orgs du user. Métrique = volume **chat Mongo**, pas coût Requesty/Langfuse.

## Utiliser le MCP MongoDB

Serveur **user-mongodb**. Outils utiles :

- **`find`** : `database: "wonkachat-prod"`, `collection: "users"`, `filter` avec `_id` (ObjectId selon syntaxe MCP)
- **`aggregate`** : même pipeline que ci-dessus pour plusieurs userIds
- **`collection-schema`** : inférer les champs d’une collection (échantillon)

## Schéma de jointure

```
userId (Langfuse)  →  users._id
users              →  organizationMemberships[].organizationId  →  organizations._id
organizations      →  name (nom affiché)
```

## Résumé

1. **userId** = `_id` dans **`users`**
2. **email** = champ **`users.email`**
3. **Organisations** = jointure **`users.organizationMemberships[].organizationId`** → **`organizations`**, nom lisible dans **`organizations.name`** (un user peut avoir plusieurs orgs)
