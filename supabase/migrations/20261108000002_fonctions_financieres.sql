-- StatementFlow — migration 002 : fonctions financières transactionnelles
--
-- Équivalents SQL des opérations atomiques du code Python v0.3.1
-- (reserve / settle / refund / activation de rechargement) :
--   * chaque fonction s'exécute dans UNE transaction SQL ;
--   * l'idempotence est garantie par les contraintes UNIQUE
--     (credit_ledger.idempotency_key, topups.transaction_id) ;
--   * un paiement ne peut JAMAIS créditer deux fois : la seconde
--     activation échoue sur topups.transaction_id unique, et le
--     grand livre refuse le doublon (key_id, idempotency_key).

-- ─────────────────────────────────────────────────────────────────────
-- Réserver atomiquement des crédits (avant traitement).
-- Retourne true si réservé, false si solde insuffisant / clé invalide.
-- Verrou la ligne de clé (for update) pour sérialiser les concurrents.
-- ─────────────────────────────────────────────────────────────────────
create or replace function reserve_credits(
    p_key_hash char(64),
    p_amount numeric,
    p_job_id text
) returns boolean as $$
declare
    v_key_id uuid;
    v_balance numeric;
begin
    if p_amount is null or p_amount <= 0 then
        raise exception 'montant de réservation invalide';
    end if;

    select id into v_key_id from api_keys
    where key_hash = lower(p_key_hash) and status = 'active'
    for update;                                   -- verrou pessimiste
    if v_key_id is null then
        return false;
    end if;

    select credits_balance into v_balance
    from api_key_balances where key_id = v_key_id;
    if v_balance is null or v_balance < p_amount then
        return false;
    end if;

    insert into credit_ledger (key_id, amount, kind, idempotency_key,
                               reference_id)
    values (v_key_id, -p_amount, 'reserve', 'job:' || p_job_id, p_job_id);
    return true;
end;
$$ language plpgsql;

-- ─────────────────────────────────────────────────────────────────────
-- Compléter une réservation (coût réel connu après traitement) ou
-- restituer (échec). Idempotent par job.
-- ─────────────────────────────────────────────────────────────────────
create or replace function settle_credits(
    p_key_hash char(64),
    p_amount numeric,          -- coût TOTAL du traitement
    p_job_id text,
    p_success boolean
) returns numeric as $$
declare
    v_key_id uuid;
    v_reserved numeric;
    v_extra numeric;
begin
    select id into v_key_id from api_keys
    where key_hash = lower(p_key_hash) and status = 'active'
    for update;
    if v_key_id is null then
        return -1;
    end if;

    select -coalesce(sum(amount), 0) into v_reserved
    from credit_ledger
    where key_id = v_key_id and kind = 'reserve'
      and idempotency_key = 'job:' || p_job_id;

    if p_success then
        v_extra := greatest(p_amount - v_reserved, 0);
        if v_extra > 0 then
            insert into credit_ledger (key_id, amount, kind,
                                       idempotency_key, reference_id)
            values (v_key_id, -v_extra, 'settle',
                    'job:' || p_job_id || ':settle', p_job_id);
        end if;
    else
        if v_reserved > 0 then
            insert into credit_ledger (key_id, amount, kind,
                                       idempotency_key, reference_id)
            values (v_key_id, v_reserved, 'refund',
                    'job:' || p_job_id || ':refund', p_job_id);
        end if;
    end if;

    return (select credits_balance from api_key_balances
            where key_id = v_key_id);
end;
$$ language plpgsql;

-- ─────────────────────────────────────────────────────────────────────
-- Activer un rechargement payé (appelée par le webhook ET le polling).
-- Transaction unique : marque le topup activé + crédite le grand livre.
-- Idempotence triple :
--   1. topups.status <> 'pending' → null (déjà traité) ;
--   2. transaction_id unique → un paiement ne peut activer deux topups ;
--   3. credit_ledger (key_id, 'topup:' || reference) unique → un seul
--      mouvement de crédit par rechargement.
-- ─────────────────────────────────────────────────────────────────────
create or replace function activate_topup(
    p_reference text,
    p_transaction_id text
) returns boolean as $$
declare
    t topups%rowtype;
begin
    if p_transaction_id is null or p_transaction_id = '' then
        raise exception 'transaction_id requis';
    end if;

    select * into t from topups
    where reference = p_reference
    for update;                                   -- verrou le rechargement
    if t.id is null then
        return false;                             -- référence inconnue
    end if;
    if t.status <> 'pending' then
        return false;                             -- déjà activé/échoué/remboursé
    end if;

    update topups
    set status = 'activated',
        transaction_id = p_transaction_id,
        activated_at = now()
    where id = t.id;

    insert into credit_ledger (key_id, amount, kind, idempotency_key,
                               reference_id)
    values (t.key_id, t.credits, 'topup', 'topup:' || t.reference,
            t.reference);
    return true;
end;
$$ language plpgsql;

-- ─────────────────────────────────────────────────────────────────────
-- Marquer un rechargement échoué (idempotent : seuls les pending).
-- ─────────────────────────────────────────────────────────────────────
create or replace function fail_topup(
    p_reference text,
    p_transaction_id text default ''
) returns boolean as $$
declare
    t topups%rowtype;
begin
    select * into t from topups where reference = p_reference for update;
    if t.id is null or t.status <> 'pending' then
        return false;
    end if;
    update topups
    set status = 'failed',
        transaction_id = coalesce(nullif(p_transaction_id, ''),
                                  transaction_id)
    where id = t.id;
    return true;
end;
$$ language plpgsql;

-- ─────────────────────────────────────────────────────────────────────
-- Journaliser un événement webhook (idempotence par event_key) :
-- l'insert échoue proprement si l'événement a déjà été reçu.
-- ─────────────────────────────────────────────────────────────────────
create or replace function record_webhook_event(
    p_event_key text,
    p_payload jsonb default '{}'::jsonb
) returns boolean as $$
begin
    insert into webhook_events (event_key, payload)
    values (p_event_key, p_payload);
    return true;                                  -- premier reçu
exception
    when unique_violation then
        return false;                             -- rejeu : déjà traité
end;
$$ language plpgsql;

-- ─────────────────────────────────────────────────────────────────────
-- Incrémenter l'usage d'une clé (après traitement réussi).
-- ─────────────────────────────────────────────────────────────────────
create or replace function record_usage(
    p_key_hash char(64),
    p_pages integer,
    p_transactions integer
) returns void as $$
begin
    insert into usage_counters (key_id, documents, pages, transactions)
    select id, 1, p_pages, p_transactions
    from api_keys where key_hash = lower(p_key_hash)
    on conflict (key_id) do update
    set documents = usage_counters.documents + 1,
        pages = usage_counters.pages + excluded.pages,
        transactions = usage_counters.transactions + excluded.transactions,
        last_seen_at = now();
end;
$$ language plpgsql;
