-- StatementFlow — migration 003 : jeu de vérification du schéma
--
-- Requêtes de contrôle à exécuter APRÈS les migrations 001 et 002
-- dans l'éditeur SQL Supabase. Chaque requête doit renvoyer le
-- résultat attendu — sinon, ne pas mettre l'API en production.

-- 1. RLS actif sur toutes les tables (8 lignes, toutes true)
select tablename, rowsecurity
from pg_tables
where schemaname = 'public'
order by tablename;
-- attendu : api_keys, audit_log, credit_ledger, jobs, organizations,
--           topups, usage_counters, webhook_events — rowsecurity = true

-- 2. Aucune policy par défaut (0 ligne : deny all pour anon/authenticated)
select tablename, policyname from pg_policies where schemaname = 'public';

-- 3. Les contraintes d'idempotence existent
select indexname from pg_indexes
where schemaname = 'public' and tablename in ('credit_ledger', 'topups',
                                              'webhook_events')
order by indexname;

-- 4. Test fonctionnel complet (à lancer, puis à annuler) :
begin;
--    4a. organisation + clé de test
insert into organizations (name) values ('test-migration')
returning id;
insert into api_keys (org_id, key_hash, label)
values ((select id from organizations where name = 'test-migration'),
        repeat('a', 64), 'cle-test')
returning id, key_hash;

--    4b. réserver 1 crédit sans solde → false
select reserve_credits(repeat('a', 64), 1, 'job-test-1');   -- attendu : false

--    4c. rechargement payé → activation → solde 500
insert into topups (reference, key_id, pack, credits, amount_fcfa, session_id)
values ('TOPUP-TESTMIGRATION',
        (select id from api_keys where key_hash = repeat('a', 64)),
        'starter', 500, 5000, 'sess-test');
select activate_topup('TOPUP-TESTMIGRATION', 'tx-test-1');  -- true
select credits_balance from api_key_balances
where key_hash = repeat('a', 64);                           -- 500.00

--    4d. ré-activation du même paiement → false (idempotence)
select activate_topup('TOPUP-TESTMIGRATION', 'tx-test-1');  -- false
select credits_balance from api_key_balances
where key_hash = repeat('a', 64);                           -- toujours 500.00

--    4e. réservation puis remboursement (échec de traitement)
select reserve_credits(repeat('a', 64), 1, 'job-test-2');   -- true
select settle_credits(repeat('a', 64), 0, 'job-test-2', false);  -- 500.00
select settle_credits(repeat('a', 64), 0, 'job-test-2', false);  -- 500.00 (idempotent)

--    4f. événement webhook rejoué → false
select record_webhook_event('transaction.success:tx-test-1');  -- true
select record_webhook_event('transaction.success:tx-test-1');  -- false

rollback;  -- le test n'écrit rien
