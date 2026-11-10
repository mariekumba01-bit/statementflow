-- ─────────────────────────────────────────────────────────────────────
-- Migration 006 — Persistance des résultats de conversion (v0.6.1)
--
-- OBJET : un redémarrage Render (redéploiement, cycle du plan free,
-- OOM-kill) vidait le dictionnaire mémoire _jobs : un résultat encore
-- dans sa période de rétention (TTL 900 s) devenait INACCESSIBLE,
-- alors que l'historique PostgreSQL (métadonnées du même job) le
-- montrait « done ». Cette migration fait de la table jobs la source
-- de vérité PENDANT le TTL uniquement :
--
--   * result jsonb : le résultat complet (transactions, contrôles,
--     usage, facturation) du job terminé avec succès ;
--   * result stocké SEULEMENT pour les jobs 'done' et purgé par la
--     maintenance existante (purge_expired_jobs supprime la LIGNE
--     entière à l'expiration : expires_at) — la politique « traitement
--     éphémère, TTL 15 minutes » est inchangée ;
--   * isolation : la lecture joint api_keys sur key_hash du
--     propriétaire (fonction get_job_result_for_key) — le résultat
--     d'un autre compte est indistinguable d'un job inconnu (NULL),
--     RLS reste actif, aucune policy ajoutée ;
--   * l'écriture est additif pur : alter table add column if not
--     exists + create or replace function. Aucune donnée existante
--     n'est modifiée.
--
-- Compatibilité : les jobs antérieurs ont result = NULL → le
-- comportement pour eux est exactement l'ancien (404 après
-- redémarrage) ; seuls les NOUVEAUX jobs bénéficient de la
-- persistance TTL.
-- ─────────────────────────────────────────────────────────────────────

alter table jobs
    add column if not exists result jsonb;

comment on column jobs.result is
    'Résultat complet de conversion (statementflow/v1) — stocké seulement pour les jobs done, purgé à expires_at (TTL 900 s), jamais au-delà';

-- Lecture isolée par propriétaire : renvoie le résultat du job si
-- (et seulement si) le job appartient à la clé, n'est pas expiré et
-- est terminé. NULL dans tous les autres cas (job inconnu, expiré,
-- autre compte, encore en traitement, en erreur) — l'appelant ne peut
-- pas distinguer les cas : pas de sonde d'existence.
-- Le statut est renvoyé à part pour distinguer processing/error/done
-- aux appels de consultation de statut (le corps d'erreur est lu dans
-- la colonne error existante).
create or replace function get_job_for_key(
    p_key_hash char(64),
    p_job_id uuid
) returns table (status text, result jsonb, error text,
                 expires_s double precision) as $$
    select j.status, j.result, j.error,
           greatest(extract(epoch from (j.expires_at - now())), 0)::float8
    from jobs j
    join api_keys k on k.id = j.key_id
    where k.key_hash = lower(p_key_hash)
      and j.id = p_job_id
      and j.expires_at > now()
$$ language sql stable;
