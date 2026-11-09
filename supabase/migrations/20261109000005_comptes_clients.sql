-- StatementFlow — migration 005 : comptes clients et analytique commerciale
--
-- Le produit v0.5.0 s'ouvre aux clients non techniques : inscription par
-- email, sessions navigateur, vérification d'email, réinitialisation de
-- mot de passe, historique et suivi marketing. Toutes les données restent
-- dans la MÊME base PostgreSQL (aucun service tiers requis).
--
-- Principes :
--   * une organisation + une clé API par compte (la clé existante porte
--     les crédits : le grand livre et les fonctions financières v0.3/0.4
--     sont réutilisés TELS QUELS) ;
--   * le mot de passe n'est JAMAIS stocké en clair : PBKDF2-HMAC-SHA256
--     (hash + sel distincts par compte) — voir statementflow/accounts.py ;
--   * les sessions/tokens ne sont stockés que hachés (SHA-256), comme
--     les clés API ;
--   * les événements marketing ne contiennent AUCUNE donnée bancaire ;
--   * RLS restrictif par défaut, comme toutes les tables existantes.

-- ─────────────────────────────────────────────────────────────────────
-- 1. Comptes utilisateurs (email + mot de passe)
-- ─────────────────────────────────────────────────────────────────────
create table if not exists users (
    id              uuid primary key default gen_random_uuid(),
    email           text not null unique,          -- minuscules (code Python)
    password_hash   text not null,                 -- pbkdf2 hex
    password_salt   text not null,                 -- sel hex par compte
    email_verified  boolean not null default false,
    verified_via    text check (verified_via in ('smtp', 'auto_no_smtp')),
    marketing_source text,                         -- direct / google / linkedin / …
    marketing_ref   text,                          -- ?ref= partenaire/campagne
    created_at      timestamptz not null default now(),
    last_login_at   timestamptz
);

-- ─────────────────────────────────────────────────────────────────────
-- 2. Sessions navigateur (cookie sf_session ; seul le hash est stocké)
-- ─────────────────────────────────────────────────────────────────────
create table if not exists sessions (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null references users(id) on delete cascade,
    token_hash  char(64) not null unique,          -- sha256 hex du token
    created_at  timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    expires_at  timestamptz not null
);

create index if not exists sessions_user_idx on sessions(user_id);
create index if not exists sessions_expiry_idx on sessions(expires_at);

-- ─────────────────────────────────────────────────────────────────────
-- 3. Tokens à usage unique (vérification email, reset mot de passe)
-- ─────────────────────────────────────────────────────────────────────
create table if not exists auth_tokens (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null references users(id) on delete cascade,
    purpose     text not null check (purpose in ('verify_email',
                                                 'password_reset')),
    token_hash  char(64) not null unique,          -- sha256 hex du token
    expires_at  timestamptz not null,
    used_at     timestamptz,
    created_at  timestamptz not null default now()
);

create index if not exists auth_tokens_user_idx on auth_tokens(user_id, purpose);

-- ─────────────────────────────────────────────────────────────────────
-- 4. Événements produit/marketing (visites, inscriptions, conversions,
--    paiements) — attribution par source et référence. Aucune donnée
--    bancaire : uniquement des compteurs et des identifiants anonymes.
-- ─────────────────────────────────────────────────────────────────────
create table if not exists events (
    id          bigint generated always as identity primary key,
    type        text not null check (type in (
                    'visit',               -- page vue (landing, démo…)
                    'signup',              -- compte créé
                    'login',               -- connexion réussie
                    'conversion_started',  -- analyse soumise
                    'conversion_done',     -- analyse réussie
                    'topup_created',       -- session de paiement créée
                    'payment_success')),   -- paiement confirmé + crédits
    anonymous_id text not null default '',
    user_id     uuid references users(id) on delete set null,
    source      text not null default 'direct',   -- attribution première
    path        text not null default '',
    metadata    jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now()
);

create index if not exists events_type_time_idx on events(type, created_at desc);
create index if not exists events_anon_idx on events(anonymous_id);
create index if not exists events_user_idx on events(user_id);

-- ─────────────────────────────────────────────────────────────────────
-- 5. Liaison compte ↔ clé API (UNE clé active par compte ; les clés
--    techniques hors compte — STATEMENTFLOW_API_KEY_HASHES — restent
--    user_id NULL et ne sont jamais exposées aux comptes)
-- ─────────────────────────────────────────────────────────────────────
alter table api_keys
    add column if not exists user_id uuid references users(id)
    on delete set null;

create index if not exists api_keys_user_idx on api_keys(user_id)
    where user_id is not null;

-- ─────────────────────────────────────────────────────────────────────
-- 6. Création atomique d'un compte : organisation + utilisateur + clé
--    API (+ bonus d'inscription éventuel via le grand livre, kind
--    'topup' → n'apparaît JAMAIS dans le chiffre d'affaires encaissé,
--    qui ne compte que les topups de pack STATUT 'activated').
-- ─────────────────────────────────────────────────────────────────────
create or replace function create_account(
    p_email         text,
    p_password_hash text,
    p_password_salt text,
    p_key_hash      char(64),
    p_bonus_credits numeric default 0,
    p_verified      boolean default false,
    p_verified_via  text default null,
    p_source        text default 'direct',
    p_ref           text default null
) returns uuid as $$
declare
    v_user_id uuid;
    v_org_id  uuid;
    v_key_id  uuid;
begin
    if exists (select 1 from users where email = lower(p_email)) then
        raise exception 'email_deja_utilise';
    end if;

    insert into organizations (name, contact_email)
    values (lower(p_email), lower(p_email))
    returning id into v_org_id;

    insert into users (email, password_hash, password_salt,
                       email_verified, verified_via,
                       marketing_source, marketing_ref)
    values (lower(p_email), p_password_hash, p_password_salt,
            p_verified, p_verified_via, p_source, p_ref)
    returning id into v_user_id;

    insert into api_keys (org_id, key_hash, label, user_id)
    values (v_org_id, lower(p_key_hash), lower(p_email), v_user_id)
    returning id into v_key_id;

    if p_bonus_credits is not null and p_bonus_credits > 0 then
        insert into credit_ledger (key_id, amount, kind,
                                   idempotency_key, reference_id)
        values (v_key_id, p_bonus_credits, 'topup',
                'signup-bonus', 'signup:' || v_user_id);
    end if;

    return v_user_id;
end;
$$ language plpgsql;

-- ─────────────────────────────────────────────────────────────────────
-- 7. Rotation de la clé d'un compte : l'ancienne clé est RÉVOQUÉE
--    (status 'revoked' — contrainte existante) et le solde est
--    TRANSFÉRÉ intégralement vers la nouvelle via deux mouvements
--    'adjustment' signés du grand livre (le solde reste la somme des
--    mouvements : aucune valeur n'est modifiée en place).
-- ─────────────────────────────────────────────────────────────────────
create or replace function rotate_user_key(
    p_user_id     uuid,
    p_new_key_hash char(64)
) returns numeric as $$
declare
    v_old_key_id uuid;
    v_new_key_id uuid;
    v_balance    numeric;
begin
    select id into v_old_key_id from api_keys
    where user_id = p_user_id and status = 'active'
    for update;
    if v_old_key_id is null then
        raise exception 'aucune_cle_active';
    end if;

    select coalesce(credits_balance, 0) into v_balance
    from api_key_balances where key_id = v_old_key_id;
    if v_balance < 0 then
        raise exception 'solde_negatif';
    end if;

    update api_keys set status = 'revoked', revoked_at = now()
    where id = v_old_key_id;

    insert into api_keys (org_id, key_hash, label, user_id)
    select org_id, lower(p_new_key_hash), label, user_id
    from api_keys where id = v_old_key_id
    returning id into v_new_key_id;

    if v_balance > 0 then
        insert into credit_ledger (key_id, amount, kind,
                                   idempotency_key, reference_id)
        values (v_old_key_id, -v_balance, 'adjustment',
                'key-rotation-out', 'rotation:' || v_old_key_id::text),
               (v_new_key_id, v_balance, 'adjustment',
                'key-rotation-in', 'rotation:' || v_old_key_id::text);
    end if;

    return v_balance;
end;
$$ language plpgsql;

-- ─────────────────────────────────────────────────────────────────────
-- 8. RLS restrictif (aucune politique : accès uniquement par le rôle
--    propriétaire de la base — jamais par une clé anon PostgREST)
-- ─────────────────────────────────────────────────────────────────────
alter table users        enable row level security;
alter table sessions     enable row level security;
alter table auth_tokens  enable row level security;
alter table events       enable row level security;
