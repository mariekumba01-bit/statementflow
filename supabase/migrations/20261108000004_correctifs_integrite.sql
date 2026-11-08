-- StatementFlow — migration 004 : correctifs d'intégrité financière
--
-- Corrige deux défauts détectés par audit dans les fonctions de la
-- migration 002 (remplacement à l'identique des signatures — safe à
-- exécuter que la 002 ait été appliquée ou non) :
--
--   1. settle_credits pouvait produire un solde NÉGATIF : après une
--      réservation de 1 crédit, un traitement coûtant N crédits était
--      débité de N-1 sans vérifier le solde restant. Le décompte est
--      désormais plafonné au solde disponible (cohérent avec le
--      comportement du magasin JSON : max(0, solde − extra)).
--
--   2. settle_credits n'était PAS idempotent : un second appel pour le
--      même job (rejeu, retry réseau) levait une violation UNIQUE au
--      lieu de retourner le solde. Le script de vérification 003
--      documentait ce comportement comme « idempotent » à tort.
--      Désormais : un job déjà soldé (settle OU refund) retourne
--      simplement le solde courant.
--
--      reserve_credits bénéficie de la même garde : re-réserver le même
--      job retourne true (déjà réservé) au lieu d'échouer sur UNIQUE.

-- ─────────────────────────────────────────────────────────────────────
-- Réservation atomique — idempotente par job.
-- ─────────────────────────────────────────────────────────────────────
create or replace function reserve_credits(
    p_key_hash char(64),
    p_amount numeric,
    p_job_id text
) returns boolean as $$
declare
    v_key_id uuid;
    v_balance numeric;
    v_exists boolean;
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

    -- idempotence : re-réserver un job déjà réservé ne débite rien
    select exists(
        select 1 from credit_ledger
        where key_id = v_key_id
          and idempotency_key = 'job:' || p_job_id
    ) into v_exists;
    if v_exists then
        return true;
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
-- Solde d'un traitement — idempotent par job, solde jamais négatif.
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
    v_balance numeric;
    v_done boolean;
begin
    select id into v_key_id from api_keys
    where key_hash = lower(p_key_hash) and status = 'active'
    for update;
    if v_key_id is null then
        return -1;
    end if;

    -- idempotence : un job déjà soldé (settle ou refund) retourne le solde
    select exists(
        select 1 from credit_ledger
        where key_id = v_key_id
          and reference_id = p_job_id
          and kind in ('settle', 'refund')
    ) into v_done;
    if v_done then
        return (select credits_balance from api_key_balances
                where key_id = v_key_id);
    end if;

    select -coalesce(sum(amount), 0) into v_reserved
    from credit_ledger
    where key_id = v_key_id and kind = 'reserve'
      and idempotency_key = 'job:' || p_job_id;

    if p_success then
        v_extra := greatest(p_amount - v_reserved, 0);
        select coalesce(credits_balance, 0) into v_balance
        from api_key_balances where key_id = v_key_id;
        -- JAMAIS de solde négatif : le décompte est plafonné au disponible
        v_extra := least(v_extra, greatest(v_balance, 0));
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

-- Le script de vérification (migration 003) reste valable : la double
-- activation reste false, le double settle/false retourne désormais le
-- solde sans erreur (comme le documentait déjà son commentaire).
