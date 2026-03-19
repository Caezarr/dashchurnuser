# Plan : Rendre la plateforme analytics consultable par tout le monde

## État actuel

Aujourd’hui, l’accès au dashboard est protégé à **deux niveaux** :

1. **Nginx (côté serveur)**  
   - Auth HTTP Basic (popup navigateur) sur tout le site.  
   - Fichier : `deploy.sh` → htpasswd, `auth_basic` dans la config Nginx.  
   - Tant que cette config est active, personne ne peut voir la page sans identifiant/mot de passe.

2. **Flask (application)**  
   - Si `AUTH_TOKEN` est défini dans `.env`, toutes les routes analytics (overview, timeseries, by-model, churn-users, etc.) exigent un token (Bearer, query `?token=`, ou cookie `rq_token`).  
   - Routes **sans** auth : `/`, `/analytics`, `/analytics/health`.  
   - Routes **avec** auth : `/analytics/overview`, `/analytics/timeseries`, `/analytics/sync`, `/analytics/export/csv`, etc.  
   - Le frontend (`dashboard.html`) affiche un écran de login et envoie ce token à chaque appel API.

Pour rendre la plateforme **consultable par tout le monde**, il faut donc agir sur Nginx et/ou Flask, et adapter le frontend.

---

## Options possibles

### Option A — Tout en lecture seule publique (le plus simple)

**Idée :** Plus aucune authentification pour la consultation. Tout le monde peut ouvrir l’URL et voir le dashboard.

| Élément        | Action |
|----------------|--------|
| **Nginx**      | Supprimer `auth_basic` et `auth_basic_user_file` pour la location `/` (ou désactiver le bloc qui les contient). |
| **Flask**      | Ne pas passer `--auth-token` au démarrage (ou laisser `AUTH_TOKEN` vide dans `.env`). Déjà supporté : si `auth_token` est `None`, le décorateur `@auth` laisse passer. |
| **Frontend**   | Si pas d’auth côté API : masquer l’écran de login et appeler directement les APIs sans token. |

**Avantages :** Mise en œuvre rapide, un seul mode “ouvert”.  
**Inconvénients :** Sync manuelle et export CSV seraient aussi accessibles à tous (si on ne les protège pas). À éviter si les données sont sensibles.

---

### Option B — Lecture publique, écriture (sync / export) protégée (recommandé)

**Idée :** Tout le monde peut **consulter** le dashboard sans login. Seules les actions sensibles (sync, export) restent protégées.

| Élément        | Action |
|----------------|--------|
| **Nginx**      | Supprimer l’auth basic sur `/` (comme en A). |
| **Flask**      | Introduire deux niveaux d’auth : (1) **aucune auth** pour les GET en lecture (overview, timeseries, by-model, by-org, churn-users, by-user) ; (2) **auth obligatoire** pour POST `/analytics/sync` et GET `/analytics/export/csv`. Soit un token “admin” dans `.env`, soit garder un seul `AUTH_TOKEN` mais n’appliquer le décorateur que sur sync et export. |
| **Frontend**   | Pas de login pour afficher les graphiques. Afficher un bouton “Sync” / “Export” qui demande le mot de passe (ou qui envoie le token admin) uniquement pour ces actions. |

**Avantages :** Bon compromis : visibilité large, contrôle sur les actions qui modifient ou exportent les données.  
**Inconvénients :** Nécessite d’adapter un peu le backend (séparer routes “lecture” et “écriture”) et le frontend (login seulement pour sync/export).

---

### Option C — Lien magique / token “lecture seule”

**Idée :** Un token ou lien spécial “view-only” permet de consulter le dashboard sans connaître le mot de passe admin. L’admin garde un token fort pour sync/export.

| Élément        | Action |
|----------------|--------|
| **Nginx**      | Soit tout le monde peut accéder à l’URL (pas d’auth basic), soit on garde une auth légère et on donne le même login “lecture” à tout le monde. |
| **Flask**      | Deux tokens en config : `AUTH_TOKEN_ADMIN` (sync, export) et `AUTH_TOKEN_VIEW` (lecture seule). Les routes GET analytics acceptent soit pas de token, soit `AUTH_TOKEN_VIEW` ; les routes sync/export n’acceptent que `AUTH_TOKEN_ADMIN`. |
| **Frontend**   | URL du type `https://domaine/?view_token=xxx`. Si `view_token` est présent et valide, le frontend l’envoie en query/cookie pour les APIs lecture seule, et n’affiche pas de boutons sync/export (ou les désactive). |

**Avantages :** On peut révoquer le lien “view” sans changer le mot de passe admin.  
**Inconvénients :** Plus de logique (deux tokens, gestion du lien dans l’URL).

---

## Plan d’implémentation recommandé : Option B

### 1. Backend (Flask) — `collector.py`

- **Garder** un seul `AUTH_TOKEN` (ou le renommer en `ADMIN_TOKEN`) utilisé **uniquement** pour :
  - `POST /analytics/sync`
  - `GET /analytics/export/csv`
- **Retirer** le décorateur `@auth` de toutes les routes **GET** en lecture seule :
  - `/analytics/overview`
  - `/analytics/timeseries`
  - `/analytics/by-model`
  - `/analytics/by-org`
  - `/analytics/usage-by-wonka-org`
  - `/analytics/churn-users`
  - `/analytics/by-user`
- La route `/analytics/health` reste sans auth (déjà le cas).
- S’assurer que `create_app` reçoit bien `auth_token` pour protéger uniquement sync et export (par exemple un décorateur `@auth_admin` appliqué seulement à ces deux routes).

### 2. Frontend — `dashboard.html` (et variantes si utilisées)

- **Détecter** si l’API exige un token : par exemple appeler `/analytics/health` et lire `auth_required`, ou simplement appeler `/analytics/overview` sans token.
- Si **auth non requise** pour la lecture :
  - Ne pas afficher l’écran de login pour l’affichage du dashboard.
  - Charger directement overview + graphiques sans token.
  - Pour “Sync” et “Export CSV” : afficher un champ mot de passe (ou popup) et n’envoyer le token que pour ces deux appels.
- Si **auth requise** (ancien comportement) : garder le flux actuel (login global, token stocké en localStorage).

### 3. Déploiement — `deploy.sh` et Nginx

- **Option “tout public”** :  
  - Ne plus créer / plus utiliser htpasswd pour la location `/`.  
  - Commenter ou supprimer les lignes `auth_basic` et `auth_basic_user_file` dans le bloc `server` HTTPS.
- **Option “toujours un peu protégé”** : garder l’auth Nginx mais avec un mot de passe très simple ou un compte “invité” partagé (moins propre qu’une vraie lecture publique).

Pour une vraie “consultation par tout le monde”, il faut **désactiver l’auth basic Nginx** sur la location qui sert le dashboard et l’API.

### 4. Sécurité et bonnes pratiques

- **Rate limiting** : garder les limites Nginx (ex. `limit_req`, `limit_conn`) pour éviter les abus.
- **HTTPS** : conserver TLS (Let’s Encrypt) comme aujourd’hui.
- **Données sensibles** : si les analytics contiennent des infos sensibles (emails, noms d’orgs, etc.), envisager soit de restreindre l’accès (Option C avec token view-only), soit de masquer/anonymiser certaines données en mode “public”.
- **Sync/export** : ne pas exposer `/analytics/sync` et `/analytics/export/csv` sans auth (Option B).

---

## Résumé des fichiers à modifier

| Fichier          | Modifications |
|------------------|----------------|
| `collector.py`   | Séparer auth “lecture” (aucune) et “admin” (sync, export) ; retirer `@auth` des routes GET lecture seule. |
| `dashboard.html`| Détection “auth requise ou non” ; pas de login pour l’affichage si lecture publique ; login uniquement pour Sync / Export si besoin. |
| `deploy.sh`      | Rendre l’auth Nginx optionnelle ou la supprimer pour la location `/` (et sous-routes) pour permettre l’accès public. |
| Config Nginx (générée par deploy) | Idem : pas d’`auth_basic` sur la location principale si on veut accès public. |

---

## Ordre des étapes suggéré

1. **Backend** : modifier `collector.py` pour que seules sync et export soient protégées ; tester en local sans `AUTH_TOKEN` puis avec.
2. **Frontend** : adapter `dashboard.html` pour mode “lecture publique” (pas de login pour les GET) + login optionnel pour sync/export.
3. **Deploy** : adapter `deploy.sh` (et la config Nginx) pour désactiver l’auth basic lorsque l’on souhaite un accès public.
4. **Vérifications** : ouvrir l’URL en navigation privée, vérifier que le dashboard s’affiche sans login, et que sync/export refusent sans token ou renvoient 401.

En suivant ce plan (Option B), la plateforme devient **consultable par tout le monde** tout en gardant la possibilité de protéger les actions sensibles (sync et export).
