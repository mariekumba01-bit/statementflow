# Supabase / PostgreSQL — persistance réelle de StatementFlow

**État : IMPLÉMENTÉ et TESTÉ (v0.4.0).** Dès que
`STATEMENTFLOW_DATABASE_URL` est défini, l'API utilise PostgreSQL pour
toutes ses données commerciales : clés gérées, soldes de crédits,
rechargements, usage, métadonnées de jobs, journal d'audit. Sans cette
variable, elle retombe sur les magasins JSON locaux (mode test).

L'adaptateur (`statementflow.pg`, classe `PgStore`) expose les MÊMES
méthodes que les magasins JSON : l'API, la CLI et les tests permutent
sans changer un appel. Les 28 tests de `tests/test_pg.py` l'exécutent
contre un vrai PostgreSQL (migrations réelles appliquées), en local
(pgserver) comme en CI (service conteneurisé postgres:16).

## Pourquoi PostgreSQL

- **Transactions ACID** pour les opérations financières (réservations,
  activations de paiements) — le verrou fichier JSON ne protège que
  les threads d'un même processus, pas plusieurs instances.
- **Idempotence structurelle** (contraintes `UNIQUE` sur
  `credit_ledger (key_id, idempotency_key)`,
  `topups.transaction_id`, `webhook_events.event_key`) : un webhook
  rejoué ou un doublon de polling ne peut pas créditer deux fois, même
  après un redémarrage entre les deux.
- **Grand livre immuable** : le solde est dérivé de la somme des
  mouvements (vue `api_key_balances`), jamais mis à jour « en place ».
- **Traçabilité** : journal d'audit, historique complet des
  opérations — exigences d'un service commercial.

## Correspondance code ↔ schéma

| Code (JSON) | PostgreSQL |
|---|---|
| `ManagedKeyStore` (`security.py`, keys.json) | `api_keys` + vue `api_key_balances` + `credit_ledger` |
| `BillingStore` (`billing.py`, billing.json) | `topups` + `webhook_events` |
| `UsageStore` (`usage.py`, usage_store.json) | `usage_counters` |
| `_jobs` (mémoire, TTL 900 s) | `jobs` (métadonnées seules, `expires_at`) |
| `reserve/settle/refund` (verrous fichier) | fonctions `reserve_credits` / `settle_credits` (verrous `for update`) |
| `activate` (statut puis crédit) | fonction `activate_topup` (UNE transaction) |
| — | `organizations`, `audit_log` |

## Brancher en 10 minutes

### 1. Récupérer la chaîne de connexion

Dashboard Supabase → **Project Settings → Database → Connection string
→ URI**. Format :
`postgresql://postgres.PROJET:PASSWORD@aws-0-REGION.pooler.supabase.com:6543/postgres`.

Pour Render, prenez la chaîne **pooler** (port 6543, mode transaction)
: elle survit aux connexions courtes d'un pool psycopg. La chaîne
directe (port 5432) fonctionne aussi pour un trafic faible.

> Projet Supabase de production : `kpkywpsflnhkkfvrhuwh`.

### 2. Appliquer les migrations (dans l'ordre)

Dashboard Supabase → **SQL Editor** → coller puis exécuter chaque
fichier de `supabase/migrations/` :

1. `20261108000001_schema_initial.sql` — tables, index, vue des
   soldes, RLS deny-all ;
2. `20261108000002_fonctions_financieres.sql` — fonctions
   transactionnelles ;
3. `20261108000003_verification.sql` — requêtes de contrôle ;
4. `20261108000004_correctifs_integrite.sql` — **correctifs d'intégrité
   (indispensables)** : `settle_credits` idempotent + plafonné au
   solde (jamais négatif), `reserve_credits` idempotent par job.

Les fichiers sont idempotents (`create or replace`, `if not exists`)
mais respectez l'ordre. Les quatre ont été exécutés et validés sur un
PostgreSQL 16 réel par la CI (job `pg-tests`) : migration du schéma,
scénario de vérification complet (réservation sans solde refusée,
activation idempotente, solde exact, rejeu sans erreur).

### 3. Vérifier (requêtes de contrôle)

```sql
-- RLS actif sur les 8 tables (rowsecurity = true partout)
select tablename, rowsecurity from pg_tables
where schemaname = 'public' order by tablename;

-- Aucune policy : deny-all pour anon/authenticated
select count(*) from pg_policies where schemaname = 'public';  -- 0

-- Scénario fonctionnel complet (n'écrit rien : rollback final)
-- voir le bloc « 4. » de la migration 003
```

### 4. Configurer Render

- API : variable d'environnement `STATEMENTFLOW_DATABASE_URL` (secret).
- Cron : même variable sur `statementflow-maintenance`.
- Redéployer : `/health` doit répondre
  `"persistence": "postgres+up"`.

### 5. Émettre les clés dans la base

Avec la CLI (mêmes commandes, autre backend) :

```bash
export STATEMENTFLOW_DATABASE_URL="postgresql://…"
statementflow keys issue "Pilote ACME" --credits 500
# → affiche la clé sf_… UNE SEULE FOIS + son hash
```

La clé en clair n'est jamais stockée : uniquement son SHA-256 dans
`api_keys.key_hash`. Les crédits initiaux passent par le grand livre.

## Sécurité

- **RLS activé sur toutes les tables, aucune policy** : aucun client
  Supabase (clé `anon`) ne peut rien lire ni écrire. Seul le processus
  serveur (chaîne complète PostgreSQL, rôle `postgres`/`service_role`)
  accède aux données ; les clients s'authentifient auprès de l'API par
  `X-API-Key`.
- **Isolation entre utilisateurs** : chaque clé API appartient à une
  organisation (`api_keys.org_id`) ; les résultats de jobs et
  rechargements sont filtrés par `key_hash` côté API (404
  indistinguable pour une référence inconnue et une référence d'autrui).
- Aucun secret n'est journalisé : l'URL de connexion est masquée
  (hôte seul) dans les messages d'erreur de `PgStore`.

## Comportement en cas de coupure

- Le pool psycopg retente **une fois** chaque opération sur une
  connexion reconstruite — sûr car toutes les mutations financières
  sont idempotentes par contrainte SQL.
- Base injoignable au démarrage : l'API démarre quand même ;
  `/health` répond `persistence: "postgres+down"`, `status:
  "degraded"` ; les routes de traitement échouent proprement (erreur
  structurée 500) sans corruption d'état.
- Repli JSON : retirez `STATEMENTFLOW_DATABASE_URL` et redéployez pour
  revenir aux magasins locaux (les données PostgreSQL restent en
  place — aucune perte).

## Limites connues

- Les **résultats d'analyse** (transactions extraites) ne sont PAS
  persistés au-delà du TTL de 900 s : politique produit « traitement
  éphémère » (aucun document bancaire stocké). Seules les métadonnées
  de traçabilité sont conservées dans `jobs`.
- `statementflow keys`/`billing` avec PostgreSQL : les commandes
  `keys issue|revoke|list|credits` passent par le même adaptateur
  quand `DATABASE_URL` est défini.
