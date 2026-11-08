# Déploiement — auto-hébergement de l'API StatementFlow

**État actuel : rien n'est déployé publiquement, et rien ne doit l'être sans les
sécurités listées ci-dessous.** Aucun abonnement, aucune dépense engagée. Ce
document distingue explicitement ce qui est prêt, ce qui nécessite une
configuration, et ce qui doit être sécurisé avant toute exposition publique.

## Ce qui est prêt (testé localement)

- **L'API elle-même** : `uvicorn statementflow.api:app` fonctionne, endpoints
  health / analyze / results / usage, OpenAPI sur `/docs`, erreurs propres
  (400/404/413/422/429/503), TTL des résultats, limites de taille.
- **L'image Docker** : `Dockerfile` fourni (python:3.12-slim + tesseract +
  poppler, utilisateur non-root, healthcheck, port dynamique `$PORT`) :
  ```bash
  docker build -t statementflow .
  docker run --rm -p 127.0.0.1:8080:8080 statementflow   # local seulement
  ```
- **Le traitement éphémère** : aucun document n'est écrit sur disque ; les
  résultats vivent en mémoire TTL 15 min.
- **Configuration par environnement** (`STATEMENTFLOW_*`) : limites, TTL, port,
  hôte, chemin des compteurs d'usage.
- **Verrouillage par défaut (v0.3.1)** : sans `STATEMENTFLOW_API_KEY_HASHES`
  ni `STATEMENTFLOW_KEYS_PATH`, l'API répond **503** sur toutes ses routes
  privées (`auth_not_configured`) et `/health` affiche
  `auth_mode: "locked"`, `status: "degraded"`. L'API n'est jamais accessible
  sans authentification ; le mode ouvert exige
  `STATEMENTFLOW_ALLOW_OPEN_AUTH=1` (développement local uniquement).

## Déploiement Render (plan gratuit)

Le blueprint `render.yaml` crée le service en un clic (New → Blueprint) :
image Docker avec **tesseract + poppler** (OCR des PDF scannés), healthcheck
sur `/health`, port dynamique `$PORT` géré par `docker/start.sh`.

1. Créer le service depuis le blueprint ; définir dans l'interface Render
   (secrets chiffrés, jamais dans le dépôt) :
   - `STATEMENTFLOW_KEYS_PATH` **ou** `STATEMENTFLOW_API_KEY_HASHES` —
     sans l'un des deux, l'API reste verrouillée (503 partout) ;
   - `STATEMENTFLOW_ADMIN_KEY` — active `/v1/admin/*` ;
   - `STATEMENTFLOW_SASPAY_SECRET_KEY` + `STATEMENTFLOW_SASPAY_WEBHOOK_SECRET`
     quand l'encaissement réel est branché (sans clé secrète : simulateur
     local, aucun paiement possible).
2. Webhook SasPay à déclarer dans le tableau de bord SasPay :
   `https://<votre-service>.onrender.com/v1/billing/webhook`.
3. **Persistance** : le plan free n'a PAS de disque persistante — les
   fichiers JSON (`keys.json`, `billing.json`) sont perdus à chaque
   redéploiement. Deux options :
   - court terme : clés déclarées par hash
     (`STATEMENTFLOW_API_KEY_HASHES`, sans état) — mais les crédits et
     l'historique de facturation ne survivent pas ;
   - **recommandé** : brancher Supabase (migrations prêtes dans
     `supabase/migrations/`, procédure dans `docs/SUPABASE.md`) puis définir
     `STATEMENTFLOW_DATABASE_URL` côté Render.

| Élément | Statut | Ce qu'il reste à faire |
|---|---|---|
| Hébergement gratuit | blueprint prêt | importer `render.yaml` dans Render — action humaine |
| Persistance | migrations prêtes | appliquer `supabase/migrations/` sur le projet Supabase dédié (voir docs/SUPABASE.md) |
| HTTPS / TLS | automatique | terminaison TLS fournie par Render |
| Domaine | non acheté | l'URL `*.onrender.com` du plan gratuit suffit pour un pilote |
| CI/CD | prêt | GitHub Actions (`.github/workflows/tests.yml`) teste chaque push/PR ; le déploiement continu n'est volontairement PAS configuré |

## Ce qui DOIT être sécurisé avant toute exposition publique

1. **Authentification réelle.** Clés gérées (`statementflow keys issue`,
   hash SHA-256 persisté) ou hashes déclarés. Le mode ouvert
   (`STATEMENTFLOW_ALLOW_OPEN_AUTH=1`) est interdit en production : il
   n'existe que pour le développement local isolé.
2. **Limite de débit / anti-abus** (429 global, pas seulement par quota) :
   à ajouter devant l'API (reverse-proxy ou middleware) selon le trafic.
3. **Taille des requêtes et durée** : `MAX_FILE_MB` est appliqué en lecture
   streamée (rejet sans tout charger en mémoire) ; vérifier aussi les
   limites du reverse-proxy.
4. **Base légale du traitement** : les relevés sont des données personnelles
   financières. Avant de traiter des documents de tiers : consentement écrit,
   rôle responsable/sous-traitant (RGPD art. 28, DPA), déclaration CDP
   (Sénégal/UEMOA) le cas échéant, durées de conservation contractualisées.
   **Sans cela, limiter strictement l'API à des documents synthétiques ou
   anonymisés.** Voir `docs/SECURITY.md`.
5. **Audit de sécurité indépendant** avant tout trafic réel.

## Recommandation pour le pilote commercial

Tant que Supabase n'est pas appliqué, le pilote reste préférable en local :
le client transmet un petit lot de documents par canal convenu, le
traitement se fait sur la machine de l'opérateur (CLI ou API locale), le
résultat est renvoyé. Cette architecture zéro-hébergement est gratuite,
conforme au budget, et n'expose aucun endpoint tant que les sécurités
ci-dessus ne sont pas en place.
