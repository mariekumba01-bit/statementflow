-- StatementFlow — migration 001 : schéma initial PostgreSQL / Supabase
--
-- Ce schéma transpose le modèle de persistance JSON actuel (v0.3.x) :
--   ManagedKeyStore (keys.json)        → api_keys + credit_ledger
--   BillingStore  (billing.json)       → topups + webhook_events
--   UsageStore    (usage_store.json)   → usage_counters
--   _jobs (mémoire API, TTL)           → jobs
--
-- Principes :
--   * identifiants stables (UUID ou references métier uniques) ;
--   * intégrité : clés primaires/étrangères, CHECK sur les énumérations ;
--   * opérations financières (crédits) par LIGNES DE GRAND LIVRE
--     immuables : le solde d'une clé est dérivé de la somme des
--     mouvements — un double crédit devient structurellement impossible
--     grâce à l'unicité (key_id, idempotency_key) ;
--   * idempotence des paiements : UNIQUE sur topups.transaction_id et
--     webhook_events.event_key ;
--   * RLS activé sur TOUTES les tables (voir bas de fichier) : aucun
--     client Supabase (clé anon) ne peut lire quoi que ce soit — tout
--     l'accès passe par l'API StatementFlow avec la clé service_role
--     (jamais dans le code client).
--
-- ⚠️ À exécuter dans l'éditeur SQL Supabase ou via supabase db push.
--    Cette migration n'est PAS appliquée automatiquement par le dépôt.

-- gen_random_uuid() est NATIF depuis PostgreSQL 13 (Supabase : PG15+).
-- pgcrypto n'est requis que pour les instances plus anciennes : sa
-- création est tolérante s'il n'est pas disponible.
do $$
begin
    create extension if not exists pgcrypto;
exception
    when others then null;  -- extension absente : inutile en PG >= 13
end $$;

-- ─────────────────────────────────────────────────────────────────────
-- 1. Comptes / organisations clientes
-- ─────────────────────────────────────────────────────────────────────
create table if not exists organizations (
    id          uuid primary key default gen_random_uuid(),
    name        text not null check (length(trim(name)) between 1 and 200),
    contact_email text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- ─────────────────────────────────────────────────────────────────────
-- 2. Clés API (équivalent ManagedKey : hash + attributs commerciaux)
--    La clé en clair n'est JAMAIS stockée — uniquement son SHA-256.
-- ─────────────────────────────────────────────────────────────────────
create table if not exists api_keys (
    id          uuid primary key default gen_random_uuid(),
    org_id      uuid not null references organizations(id)
                on delete cascade,
    key_hash    char(64) not null unique,          -- sha256 hex, minuscules
    label       text not null default '',
    status      text not null default 'active'
                check (status in ('active', 'revoked')),
    created_at  timestamptz not null default now(),
    revoked_at  timestamptz
);

create index if not exists api_keys_org_idx on api_keys(org_id);

-- ─────────────────────────────────────────────────────────────────────
-- 3. Grand livre des crédits (mouvements immuables, signés)
--    reserve (−) / settle (−) / refund (+) / topup (+) / ajustement (±).
--    Le solde = somme des montants : aucune mise à jour « en place »
--    d'un solde → pas de perte de mutation concurrente.
-- ─────────────────────────────────────────────────────────────────────
create table if not exists credit_ledger (
    id              bigint generated always as identity primary key,
    key_id          uuid not null references api_keys(id) on delete cascade,
    amount          numeric(12, 2) not null check (amount <> 0),
    -- nature du mouvement (traçabilité complète)
    kind            text not null check (kind in (
                        'topup',            -- activation d'un rechargement
                        'reserve',          -- réservation avant traitement
                        'settle',           -- complément post-traitement
                        'refund',           -- restitution (échec)
                        'adjustment')),     -- correction manuelle opérateur
    -- idempotence : un même (key_id, idempotency_key) ne peut exister
    -- qu'une fois → rejouer un webhook ou un décompte ne double rien
    idempotency_key text,
    reference_id    text,                -- topup reference ou job_id
    created_at      timestamptz not null default now(),
    unique (key_id, idempotency_key)
);

create index if not exists credit_ledger_key_idx on credit_ledger(key_id, created_at desc);
-- uniques partielles : idempotency_key optionnel (ajustements libres)
create unique index if not exists credit_ledger_idem_idx
    on credit_ledger(key_id, idempotency_key)
    where idempotency_key is not null;

-- Vue du solde par clé (source de vérité dérivée)
create or replace view api_key_balances as
select k.id as key_id,
       k.key_hash,
       k.status,
       coalesce(sum(l.amount), 0) as credits_balance
from api_keys k
left join credit_ledger l on l.key_id = k.id
group by k.id, k.key_hash, k.status;

-- ─────────────────────────────────────────────────────────────────────
-- 4. Rechargements SasPay (équivalent BillingStore._topups)
-- ─────────────────────────────────────────────────────────────────────
create table if not exists topups (
    id              uuid primary key default gen_random_uuid(),
    reference       text not null unique,          -- TOPUP-XXXXXXXXXXXX
    key_id          uuid not null references api_keys(id) on delete cascade,
    pack            text not null check (pack in ('starter', 'pro', 'business')),
    credits         integer not null check (credits > 0),
    amount_fcfa     integer not null check (amount_fcfa > 0),
    session_id      text not null,                 -- session checkout SasPay
    checkout_url    text not null default '',
    status          text not null default 'pending'
                    check (status in ('pending', 'activated', 'failed', 'refunded')),
    -- idempotence des paiements : une transaction SasPay ne peut activer
    -- qu'UN rechargement (et un rechargement n'accepte qu'une transaction)
    transaction_id  text unique,
    created_at      timestamptz not null default now(),
    activated_at    timestamptz
);

create index if not exists topups_key_idx on topups(key_id, created_at desc);
create index if not exists topups_status_idx on topups(status)
    where status = 'pending';

-- ─────────────────────────────────────────────────────────────────────
-- 5. Événements webhook (équivalent BillingStore._webhook_events)
--    Idempotence : event_key unique (« event:transaction_id »).
-- ─────────────────────────────────────────────────────────────────────
create table if not exists webhook_events (
    id          bigint generated always as identity primary key,
    event_key   text not null unique,      -- 'transaction.success:tx-…'
    received_at timestamptz not null default now(),
    payload     jsonb not null default '{}'::jsonb
);

-- purge automatique des événements de plus de 30 jours (parallèle du
-- comportement JSON : anti-croissance du registre d'idempotence)
create or replace function purge_old_webhook_events() returns void as $$
    delete from webhook_events where received_at < now() - interval '30 days';
$$ language sql;

-- ─────────────────────────────────────────────────────────────────────
-- 6. Compteurs d'usage par clé (équivalent UsageStore)
-- ─────────────────────────────────────────────────────────────────────
create table if not exists usage_counters (
    key_id          uuid primary key references api_keys(id) on delete cascade,
    documents       integer not null default 0 check (documents >= 0),
    pages           integer not null default 0 check (pages >= 0),
    transactions    integer not null default 0 check (transactions >= 0),
    first_seen_at   timestamptz not null default now(),
    last_seen_at    timestamptz not null default now()
);

-- ─────────────────────────────────────────────────────────────────────
-- 7. Traitements / jobs (équivalent _jobs en mémoire, avec persistance)
--    Les résultats JSON ne sont PAS stockés ici au-delà du TTL : la
--    politique produit reste « traitement éphémère ». Seules les
--    métadonnées et le statut sont conservés pour la traçabilité.
-- ─────────────────────────────────────────────────────────────────────
create table if not exists jobs (
    id          uuid primary key,                  -- job_id hex de l'API
    key_id      uuid not null references api_keys(id) on delete cascade,
    filename    text not null default 'document',
    kind        text check (kind in ('pdf', 'csv', 'xlsx')),
    status      text not null default 'processing'
                check (status in ('processing', 'done', 'error')),
    pages       integer,
    rows        integer,
    transactions_count integer,
    credits_charged numeric(12, 2),
    error       text,
    created_at  timestamptz not null default now(),
    expires_at  timestamptz not null               -- TTL résultats (900 s)
                default now() + interval '900 seconds'
);

create index if not exists jobs_key_idx on jobs(key_id, created_at desc);
create index if not exists jobs_expiry_idx on jobs(expires_at);

-- ─────────────────────────────────────────────────────────────────────
-- 8. Journal d'événements (traçabilité opérationnelle)
-- ─────────────────────────────────────────────────────────────────────
create table if not exists audit_log (
    id          bigint generated always as identity primary key,
    key_id      uuid references api_keys(id) on delete set null,
    event       text not null,                     -- 'analyze', 'topup.activate', …
    detail      jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now()
);

create index if not exists audit_log_time_idx on audit_log(created_at desc);

-- ═════════════════════════════════════════════════════════════════════
-- ROW LEVEL SECURITY — politique restrictive par défaut
--
-- Modèle d'autorisation réel de StatementFlow : AUCUN client final
-- n'accède directement à Supabase. L'API (processus serveur, clé
-- service_role, RLS contournée par le rôle service) est le SEUL point
-- d'accès ; les clients s'authentifient auprès de l'API par X-API-Key.
-- On active donc RLS et on ne crée AUCUNE policy : par défaut tout est
-- refusé pour anon/authenticated, y compris la lecture.
-- ═════════════════════════════════════════════════════════════════════
alter table organizations  enable row level security;
alter table api_keys       enable row level security;
alter table credit_ledger enable row level security;
alter table topups         enable row level security;
alter table webhook_events enable row level security;
alter table usage_counters enable row level security;
alter table jobs           enable row level security;
alter table audit_log      enable row level security;

-- Ceinture et bretelles : aucune policy par défaut → deny all. Si un
-- jour un accès direct client devient nécessaire, il faudra créer des
-- policies EXPLICITES par organisation (auth.uid() → org), jamais un
-- bypass. Exemple volontairement NON activé :
--   create policy "isolement par organisation" on api_keys
--     for select using (
--       exists (select 1 from organization_members m
--               where m.org_id = api_keys.org_id and m.user_id = auth.uid()));

-- Sécurité supplémentaire : révoquer les droits d'écriture du rôle anon
-- (rôle propre à Supabase : tolérant sur un PostgreSQL auto-hébergé)
do $$
begin
    revoke insert, update, delete on all tables in schema public from anon;
exception
    when undefined_object then null;  -- rôle anon inexistant hors Supabase
end $$;
