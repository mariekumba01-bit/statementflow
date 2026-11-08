# Cron Job Render — maintenance automatique StatementFlow

Le service **`statementflow-maintenance`** (type `cron` dans
`render.yaml`) exécute les tâches périodiques sans dépendre d'une
requête utilisateur. Il est **indépendant de l'API** (service Render
séparé, image Docker identique, commande différente).

**État : NON DÉPLOYÉ.** Le service Render Cron n'est pas créé : son
bloc dans `render.yaml` est **commenté** car les Cron Jobs Render
exigent un plan payant — aucune dépense n'est engagée sans décision
explicite (voir « Activation » plus bas). Le code est complet, testé
(15 tests, dont réconciliation et verrou) et exécutable manuellement.

## Commande

```bash
python -m statementflow.maintenance
```

(`dockerCommand` du service cron une fois activé — l'image est le
même Dockerfile que l'API, avec les extras `api` et `pg`.)

## Test manuel gratuit (recommandé avant toute activation)

Sans aucun service Render payant, depuis n'importe quel poste avec
Python et le dépôt :

```bash
export STATEMENTFLOW_DATABASE_URL="postgresql://…(chaîne Supabase)"
# optionnel — sans cette clé, la réconciliation SasPay est sautée :
export STATEMENTFLOW_SASPAY_SECRET_KEY="sk_live_…"
python -m statementflow.maintenance
echo $?   # 0 nominal · 2 anomalie financière critique · 3 erreur technique
```

Le rapport s'affiche ligne par ligne (une ligne JSON par tâche) et
l'exécution est journalisée dans `audit_log`. Aucune clé d'API n'est
requise ; aucun secret n'apparaît dans les sorties (DSN masqué).

## Les quatre tâches

### A. Nettoyage
- Suppression des **métadonnées de jobs expirées** (TTL `expires_at`,
  parallèle persistant du TTL mémoire de 900 s de l'API).
- Purge des **événements webhook de plus de 30 jours** (registre
  d'idempotence — anti-croissance, même règle que le mode JSON).
- Les résultats bancaires ne sont jamais stockés (politique
  « traitement éphémère ») : il n'y a rien d'autre à purger, et aucun
  fichier temporaire n'est écrit par l'API (traitement en mémoire).

### B. Réconciliation SasPay
- Rechargements `pending` de plus de `STATEMENTFLOW_RECONCILE_AFTER_HOURS`
  (défaut 1 h) : interrogation du **statut réel** chez la passerelle
  (`GET /checkout-sessions/{id}/status/` — jamais un statut mémorisé).
- `PAID` → activation via la même fonction transactionnelle que le
  webhook (idempotente par statut + transaction unique) ; `FAILED` →
  marquage échec ; sinon laissé en attente.
- Sessions injoignables → statut `warn` dans le rapport (une
  passerelle injoignable de façon répétée est un signal d'alerte).

### C. Intégrité financière (détection uniquement)
- Soldes négatifs (théoriquement impossibles).
- Rechargements **activés sans crédit** au grand livre.
- Crédits **sans paiement valide** correspondant.
- Ces incohérences sont signalées `CRITIQUE` (code de sortie 2, visible
  dans les logs Render) — **jamais corrigées automatiquement** : la
  correction financière reste une décision humaine (voir
  « Procédure de récupération » dans DEPLOYMENT.md).

### D. Surveillance
- Ping PostgreSQL (`postgres_ok` dans le rapport).
- Chaque exécution est journalisée dans `audit_log` (`maintenance.run`
  avec le rapport complet en JSON).
- Code de sortie : 0 nominal, 2 anomalie critique, 3 échec technique.

## Fréquence : pourquoi horaire (`15 * * * *`)

- Les **renvois de webhook SasPay** s'étalent sur ~2 h 30 après l'événement
  (immédiat, +30 s, +5 min, +30 min, +2 h — 5 tentatives). Une
  réconciliation horaire rattrape tout événement manqué en **moins
  d'une heure**.
- Le **polling client** (`/v1/billing/status/{ref}`) reste le mécanisme
  temps réel : le client qui attend sa recharge n'attend jamais le cron.
- Le nettoyage serait suffisant au quotidien, mais il est gratuit
  (requêtes indexées) et idempotent.
- `15` minutes après l'heure pile : évite la contention des tâches
  planifiées à l'heure exacte (usage courant).

## Exécutions simultanées, interruptions, reprise

- **Verrou consultatif** `pg_try_advisory_lock` sur une connexion
  dédiée : deux exécutions simultanées sont impossibles ; une
  exécution interrompue libère le verrou avec sa connexion (le cron
  suivant reprend proprement).
- **Chaque tâche est idempotente** : purges par critère temporel,
  activations par fonction idempotente, détections sans effets. Une
  exécution interrompue puis rejouée ne double rien.
- Durée typique : quelques secondes (requêtes indexées + un appel
  passerelle par rechargement pending ancien).

## Variables d'environnement (minimales)

| Variable | Requis | Rôle |
| --- | --- | --- |
| `STATEMENTFLOW_DATABASE_URL` | oui | persistance (sans elle : sortie 0, rien à maintenir) |
| `STATEMENTFLOW_SASPAY_SECRET_KEY` | non | réconciliation réelle (sinon tâche B sautée proprement) |
| `STATEMENTFLOW_RECONCILE_AFTER_HOURS` | non | âge minimum des pending réconciliés (défaut 1) |

Aucune clé d'API, aucun secret de signature webhook : le cron n'expose
rien et ne fait qu'interroger. Aucun secret n'est journalisé (rapport =
compteurs et références).

## Plan et coût Render — activation (action explicite requise)

⚠️ **Les Cron Jobs Render ne sont pas disponibles sur le plan free**
(minimum 0.5 CPU / 512 MB, plan `starter`). Facturation à l'usage :
~24 exécutions/jour de quelques secondes — de l'ordre de quelques
dollars par mois. **Aucune dépense n'est engagée à ce jour** : le bloc
du service est commenté dans `render.yaml`. Pour l'activer :
1. valider le coût estimé ci-dessus ;
2. décommenter le bloc `statementflow-maintenance` dans `render.yaml` ;
3. importer/synchroniser le blueprint Render et définir
   `STATEMENTFLOW_DATABASE_URL` sur le service cron.

Le cron est **optionnel** : sans lui, webhooks + polling client
suffisent à activer les crédits ; le cron n'est qu'un filet de
sécurité (rattrapage + intégrité). En attendant, exécutez le test
manuel gratuit ci-dessus à la fréquence voulue.

## Exécution manuelle (test, rattrapage)

```bash
# même environnement que le service (DATABASE_URL requis)
python -m statementflow.maintenance
echo $?   # 0 = nominal, 2 = anomalie critique (voir logs), 3 = échec
```

Les tests automatiques (`tests/test_pg.py::TestMaintenance`) couvrent :
cycle nominal, rattrapage d'un paiement manqué (activation, sans
double crédit au rejeu), échec passerelle, détection d'anomalie
critique (code 2), sortie sans base de données.
