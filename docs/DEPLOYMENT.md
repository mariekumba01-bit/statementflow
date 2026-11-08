# Déploiement — StatementFlow sur Render + Supabase

**État actuel (v0.4.0)** : l'API est déployée sur Render
(`https://statementflow-api-ch0f.onrender.com`, service
`statementflow-api`, plan free, autoDeploy) ; la persistance
PostgreSQL/Supabase est **implémentée et testée** — il suffit de
définir `STATEMENTFLOW_DATABASE_URL` pour l'activer ; le Cron Job de
maintenance est prêt dans le blueprint (plan payant requis par Render).

## Architecture déployée

```
Client (X-API-Key) ──HTTPS──> Render : statementflow-api (Docker)
                                  │
                                  ├── Supabase PostgreSQL (données commerciales)
                                  │     clés, crédits (grand livre), rechargements,
                                  │     usage, jobs, audit — si DATABASE_URL
                                  ├── SasPay (encaissement, si SECRET_KEY)
                                  │     checkout + webhook signé HMAC
                                  └── Render Cron : statementflow-maintenance
                                        nettoyage, réconciliation, intégrité
```

- **API** : image Docker (python:3.12-slim + tesseract + poppler pour
  l'OCR, extras `api` + `pg`), port dynamique `$PORT` via
  `docker/start.sh`, healthcheck `/health`, traitement en mémoire
  (aucun document écrit sur disque), résultats TTL 900 s.
- **Persistance** : PostgreSQL via `STATEMENTFLOW_DATABASE_URL`
  (adaptateur `statementflow.pg`, procédure complète dans
  `docs/SUPABASE.md`). Sans cette variable : magasins JSON locaux —
  sur le plan free le disque est éphémère (état perdu à chaque
  redéploiement), à réserver aux tests.
- **Cron** : `statementflow-maintenance` (voir `docs/CRON.md`) —
  nettoyage, réconciliation SasPay, intégrité financière,
  surveillance. ⚠️ plan payant obligatoire (Render ne propose pas de
  cron free) ; le service est optionnel (webhooks + polling client
  suffisent à activer les crédits).

## Variables d'environnement Render

| Variable | Requis | Rôle |
|---|---|---|
| `STATEMENTFLOW_DATABASE_URL` | **recommandé** | persistance PostgreSQL (Supabase). Sans elle : JSON éphémère |
| `STATEMENTFLOW_KEYS_PATH` **ou** `STATEMENTFLOW_API_KEY_HASHES` | oui (prod) | sans l'un des deux, routes privées verrouillées (503) — avec `DATABASE_URL`, les clés gérées vivent dans la base : définir `STATEMENTFLOW_KEYS_PATH=/data/keys.json` n'est plus nécessaire, l'authentification fermée est assurée par la base |
| `STATEMENTFLOW_ADMIN_KEY` | recommandé | active `/v1/admin/*` (usage global, reset) |
| `STATEMENTFLOW_SASPAY_SECRET_KEY` | pour l'encaissement | `sk_live_…` — sans elle : simulateur, aucun paiement réel |
| `STATEMENTFLOW_SASPAY_WEBHOOK_SECRET` | pour l'encaissement | vérification HMAC des webhooks |
| `STATEMENTFLOW_MAX_FILE_MB` / `_MAX_PAGES` / `_MAX_ROWS` | non | limites (20 / 200 / 200 000 par défaut) |
| `STATEMENTFLOW_RESULTS_TTL_S` | non | TTL résultats (900 s) |

Le webhook SasPay à déclarer dans le tableau de bord SasPay :
`https://statementflow-api-ch0f.onrender.com/v1/billing/webhook`
(événements `transaction.success`, `transaction.failed`).

## Déployer / mettre à jour

1. Le push sur `main` déclenche GitHub Actions (tests sur 3 versions
   de Python + job PostgreSQL + build + audit) ;
2. Render autoDeploy reconstruit l'image (`pip install ".[api,pg]"`)
   et remplace le service ;
3. Vérifier : `curl https://statementflow-api-ch0f.onrender.com/health`
   → `{"status": "ok", "persistence": "postgres+up", …}` ;
4. En cas d'échec de build : corriger avant de laisser le service
   indisponible (le déploiement précédent reste actif pendant le
   build ; Render bascule seulement si le nouveau conteneur démarre).

## Santé et supervision

- `/health` : `status` (`ok`/`degraded`), `auth_mode`, `persistence`
  (`json` / `postgres+up` / `postgres+down`), limites.
- Cron : logs du service `statementflow-maintenance` (rapport JSON
  par tâche, code de sortie 2 = anomalie critique → échec visible
  dans le dashboard Render).
- Base : onglet Supabase (logs PostgreSQL, usage).

## Procédure de récupération après incident

### Base injoignable / `persistence: postgres+down`
1. Vérifier Supabase (dashboard → Database → status) ;
2. l'API tourne toujours (traitement refusé proprement : 500
   structuré, aucune corruption) ;
3. une fois la base revenue : `/health` repasse `postgres+up` au
   sondage suivant — aucune donnée n'a été perdue (opérations
   idempotentes, grand livre immuable).

### Webhook SasPay manqué (client payé non crédité)
1. le client peut forcer le polling : `GET /v1/billing/status/{ref}`
   (interroge le statut réel et active) ;
2. sinon le cron horaire rattrape tout pending de plus d'1 h ;
3. vérifier `topups` : `select * from topups where status='pending'`
   — toute référence ancienne non résolue = incident à examiner.

### Incohérence financière signalée par le cron (code 2)
Le rapport liste précisément (soldes négatifs / crédits sans paiement
/ paiements sans crédit). **La correction est manuelle** :
1. identifier le mouvement en cause (`credit_ledger` par
   `reference_id`) ;
2. si un crédit manque : insérer un mouvement `topup` avec
   `idempotency_key = 'topup:' || reference` (jamais un UPDATE du
   solde — le solde est dérivé) ;
3. journaliser la décision dans `audit_log` ;
4. relancer `python -m statementflow.maintenance` → doit repasser à 0.

### Mauvais déploiement
Render → deploys → rollback (l'image précédente redémarre). Le schéma
PostgreSQL étant migré à part (migrations SQL explicites), un rollback
de l'API n'affecte pas les données.

## Ce qui reste à sécuriser avant un trafic commercial réel

1. **Limite de débit globale** (429) : à ajouter devant l'API selon le
   trafic (reverse-proxy ou middleware) — les quotas par clé existent
   déjà.
2. **Base légale du traitement** : les relevés sont des données
   personnelles financières (consentement, rôle responsable/
   sous-traitant, déclaration CDP Sénégal/UEMOA le cas échéant).
3. **Compte marchand SasPay live** (KYC) : sans lui, l'encaissement
   reste en mode simulateur.
4. **Audit de sécurité indépendant** avant tout trafic réel.
