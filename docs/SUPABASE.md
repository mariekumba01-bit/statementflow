# Migration Supabase / PostgreSQL — StatementFlow

**État : migrations PRÊTES mais NON APPLIQUÉES.** Cette page décrit le
schéma préparé, comment l'appliquer sur le projet Supabase dédié, et la
correspondance exacte avec le code actuel. Aucune migration n'a été
exécutée sur une base réelle depuis ce dépôt : l'application est une
action opérateur (voir « Procédure » ci-dessous).

## Pourquoi migrer

La persistance actuelle (fichiers JSON : `keys.json`, `billing.json`,
`usage_store.json`) est fiable pour un poste unique, pas pour un service
hébergé multi-instances (Render Free redémarre les processus ; deux
instances de l'API ne peuvent pas partager un fichier). PostgreSQL via
Supabase apporte :

- des **transactions ACID** pour les opérations financières (crédits,
  activations de paiements) — le verrou fichier actuel ne protège que
  les threads d'un même processus ;
- une **idempotence structurelle** (contraintes `UNIQUE`) : un webhook
  rejoué ou un doublon de polling ne peut pas créditer deux fois, même
  après un redémarrage entre les deux ;
- la **traçabilité** (grand livre immuable des crédits, journal
  d'audit) exigée par un service commercial.

## Correspondance code ↔ schéma

| Code actuel (v0.3.1) | Table / fonction SQL |
|---|---|
| `ManagedKeyStore` (`security.py`, keys.json) | `api_keys` + vue `api_key_balances` |
| `ManagedKeyStore.reserve()` | `reserve_credits()` (verrou `for update`) |
| `ManagedKeyStore.settle()` / `refund()` | `settle_credits()` |
| `BillingStore.activate()` + `add_credits()` | `activate_topup()` (transaction unique) |
| `BillingStore.mark_failed()` | `fail_topup()` |
| `BillingStore.mark_webhook()` / `webhook_seen()` | `record_webhook_event()` + `webhook_events.event_key` UNIQUE |
| `UsageStore.record()` | `record_usage()` (upsert atomique) |
| `BillingStore.create_topup()` | insert `topups` |
| `_jobs` (mémoire, TTL 900 s) | `jobs` (métadonnées seulement, `expires_at`) |
| — (nouveau) | `organizations`, `credit_ledger`, `audit_log` |

## Choix de conception

1. **Solde dérivé, jamais stocké.** Le solde d'une clé est la somme des
   mouvements du grand livre `credit_ledger` (vue `api_key_balances`).
   Un mouvement est une insertion immuable : aucune mise à jour « en
   place » d'un solde → aucune mutation concurrente perdue.
2. **Idempotence par contraintes, pas par code.** `topups.transaction_id`
   est UNIQUE : la seconde activation d'un même paiement SasPay échoue
   en base. `credit_ledger (key_id, idempotency_key)` est UNIQUE : le
   même job ou le même rechargement ne peut produire deux mouvements.
3. **RLS partout, zéro policy.** Le modèle d'autorisation réel est :
   les clients s'authentifient auprès de l'API StatementFlow
   (X-API-Key) ; **aucun client n'accède directement à Supabase**.
   RLS est donc activé sur toutes les tables sans aucune policy → tout
   est refusé pour `anon`/`authenticated`. Seul le rôle `service_role`
   (utilisé par l'API côté serveur, jamais exposé) contourne RLS.
4. **Aucun secret dans le dépôt.** L'URL et la clé `service_role`
   vivent dans les variables d'environnement de l'hébergeur
   (`STATEMENTFLOW_DATABASE_URL`), jamais dans le code ni dans GitHub.
5. **Les documents restent éphémères.** La table `jobs` ne stocke que
   les métadonnées et statuts (traçabilité), pas les relevés ni les
   transactions extraites : la politique produit « aucun document
   conservé » est préservée.

## Procédure d'application (action opérateur)

1. Créer le projet Supabase dédié (région UE de préférence, données
   financières francophones → RGPD/CDP).
2. Éditeur SQL → exécuter dans l'ordre :
   - `supabase/migrations/20261108000001_schema_initial.sql`
   - `supabase/migrations/20261108000002_fonctions_financieres.sql`
3. Exécuter `supabase/migrations/20261108000003_verification.sql` :
   chaque requête commentée doit renvoyer le résultat attendu
   (RLS actif, 0 policy, tests fonctionnels dans une transaction
   `begin…rollback` qui n'écrit rien).
4. Configurer l'API (Render) : `STATEMENTFLOW_DATABASE_URL` avec la
   chaîne de connexion **service_role** (Settings → Database).

## Ce que cette migration NE fait PAS

- Elle ne migre pas automatiquement les données JSON existantes
  (pilote local) : un script d'import sera fourni si le pilote doit
  être basculé ; à ce jour le volume pilote est re-émissible via
  `statementflow keys issue`.
- Elle ne modifie pas le code Python : l'adaptation de
  `ManagedKeyStore`/`BillingStore` vers PostgreSQL est le chantier
  suivant (une interface stockage est déjà implicite : mêmes méthodes
  `reserve/settle/refund/activate` — les fonctions SQL portent les
  mêmes noms pour faciliter l'adaptateur).
