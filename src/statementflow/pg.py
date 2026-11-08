"""Adaptateur PostgreSQL / Supabase : persistance réelle des données
commerciales (v0.4.0, voir docs/SUPABASE.md).

Remplace les magasins JSON (ManagedKeyStore, BillingStore, UsageStore)
quand STATEMENTFLOW_DATABASE_URL est défini : MÊMES méthodes publiques,
MÊMES sémantiques — l'API, la CLI et les tests permutent entre JSON et
PostgreSQL sans changer les appels.

Architecture :
- psycopg 3 + pool de connexions (psycopg_pool) ;
- les opérations financières passent par les FONCTIONS transactionnelles
  du schéma (reserve_credits, settle_credits, activate_topup, …) :
  verrous pessimistes « for update », idempotence par contraintes UNIQUE
  (credit_ledger (key_id, idempotency_key), topups.transaction_id,
  webhook_events.event_key) ;
- le solde est DÉRIVÉ du grand livre immuable (vue api_key_balances) :
  aucune mise à jour « en place » → pas de perte de mutation concurrente,
  et un double crédit devient structurellement impossible ;
- reconnexion : une opération qui échoue sur une connexion périmée est
  retentée UNE fois sur une connexion reconstruite — sûr car toutes les
  mutations financières sont idempotentes par contrainte ;
- aucune clé secrète n'est journalisée : l'URL de connexion est masquée
  dans les messages d'erreur (hôte seul, jamais le mot de passe).

Schéma requis : supabase/migrations/*.sql appliqués dans l'ordre (voir
docs/SUPABASE.md — éditeur SQL Supabase ou supabase db push). L'API ne
crée PAS le schéma elle-même.

Variables d'environnement :
- STATEMENTFLOW_DATABASE_URL : postgresql://… (chaîne complète Supabase,
  côté serveur uniquement) ;
- STATEMENTFLOW_PG_POOL_MIN / _MAX (défauts 0 / 4).
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

try:
    import psycopg
    from psycopg_pool import ConnectionPool, PoolTimeout
except ImportError:  # pragma: no cover — psycopg est optionnel (mode JSON)
    psycopg = None
    ConnectionPool = None
    PoolTimeout = Exception

ENV_PREFIX = 'STATEMENTFLOW_'
POOL_TIMEOUT_S = 15.0

# marqueur d'activation combinée : activate() crédite DÉJÀ le grand livre
# (fonction SQL transactionnelle) — l'appelant ne doit PAS re-créditer.
COMBINED_CREDIT_ACTIVATION = True


class PgStoreError(RuntimeError):
    """Erreur de persistance PostgreSQL (message sans secret)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _iso(dt) -> str:
    if dt is None:
        return ''
    if isinstance(dt, str):
        return dt
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _mask_dsn(dsn: str) -> str:
    """URL de connexion masquée pour les messages d'erreur."""
    try:
        if '://' in dsn:
            head, rest = dsn.split('://', 1)
            host = rest.split('@', 1)[-1].split('/', 1)[0].split('?', 1)[0]
            return f'{head}://***@{host}'
        return 'postgresql (socket UNIX)'
    except Exception:
        return 'postgresql'


class PgStore:
    """Magasin PostgreSQL : clés gérées, crédits, rechargements, usage.

    Interface compatible ManagedKeyStore + BillingStore (mêmes noms de
    méthodes, mêmes sémantiques) + extensions maintenance (cron). Toutes
    les opérations sont de courtes transactions autonomes.
    """

    def __init__(self, dsn: str, pool_min: int = 0, pool_max: int = 4):
        if psycopg is None:
            raise PgStoreError(
                'psycopg non installé : pip install "psycopg[binary,pool]"')
        self._dsn = dsn
        self._pool_min, self._pool_max = int(pool_min), int(pool_max)
        self._pool_lock = threading.Lock()
        self._pool: Optional['ConnectionPool'] = None

    @property
    def dsn(self) -> str:
        """DSN de connexion (pour les connexions dédiées — verrou cron)."""
        return self._dsn

    # ── pool de connexions ────────────────────────────────────────
    @property
    def pool(self) -> 'ConnectionPool':
        pool = self._pool
        if pool is not None and not pool.closed:
            return pool
        with self._pool_lock:
            if self._pool is None or self._pool.closed:
                try:
                    self._pool = ConnectionPool(
                        self._dsn, min_size=self._pool_min,
                        max_size=self._pool_max, timeout=POOL_TIMEOUT_S,
                        open=True)
                except Exception as e:
                    self._pool = None
                    raise PgStoreError(
                        f'PostgreSQL injoignable ({_mask_dsn(self._dsn)}) : '
                        f'{type(e).__name__}') from None
            return self._pool

    def _call(self, fn):
        """Exécute fn(conn) sur une connexion du pool ; UNE relance si la
        connexion était périmée (les mutations SQL étant idempotentes, un
        rejeu après coupure ne double jamais un mouvement financier)."""
        try:
            with self.pool.connection() as conn:
                return fn(conn)
        except (PoolTimeout, psycopg.OperationalError):
            self._reset_pool()
            with self.pool.connection() as conn:
                return fn(conn)

    def _reset_pool(self):
        with self._pool_lock:
            pool = self._pool
            self._pool = None
            try:
                if pool is not None:
                    pool.close()
            except Exception:
                pass

    def close(self):
        with self._pool_lock:
            pool = self._pool
            self._pool = None
            if pool is not None and not pool.closed:
                pool.close()

    def ping(self) -> bool:
        """Sonde la base (true/false) — /health et cron."""
        try:
            self._call(lambda c: c.execute('select 1'))
            return True
        except Exception:
            return False

    # ── interface ManagedKeyStore ─────────────────────────────────
    @property
    def path_configured(self) -> bool:
        """Toujours True : un magasin PostgreSQL est TOUJOURS configuré
        (authentification fermée, comme KEYS_PATH)."""
        return True

    def lookup(self, key_hash: str) -> Optional[dict]:
        """Clé gérée valide (présente, active) ou None — dict compatible
        ManagedKey (label, key_hash, credits, revoked, created)."""
        def q(conn):
            row = conn.execute(
                'select k.label, k.key_hash, k.status, k.created_at,'
                ' coalesce(b.credits_balance, 0)'
                ' from api_keys k'
                ' left join api_key_balances b on b.key_id = k.id'
                ' where k.key_hash = %s', (key_hash,)).fetchone()
            if row is None or row[2] != 'active':
                return None
            return {'label': row[0], 'key_hash': row[1],
                    'credits': float(row[4]), 'revoked': False,
                    'created': _iso(row[3])}
        return self._call(q)

    def balance(self, key_hash: str) -> Optional[float]:
        def q(conn):
            row = conn.execute(
                'select credits_balance from api_key_balances'
                ' where key_hash = %s', (key_hash,)).fetchone()
            return None if row is None else float(row[0])
        return self._call(q)

    def is_revoked(self, key_hash: str) -> bool:
        def q(conn):
            row = conn.execute(
                'select status from api_keys where key_hash = %s',
                (key_hash,)).fetchone()
            return row is not None and row[0] != 'active'
        return self._call(q)

    def issue(self, label: str, credits: float = 0.0,
              org_name: str = 'default') -> tuple[str, dict]:
        """Émet une clé : (clé_en_clair, enregistrement-dict). La clé en
        clair n'est jamais persistée — uniquement son SHA-256. Les crédits
        initiaux passent par le grand livre (mouvement 'topup')."""
        from .security import generate_key, hash_key
        key = generate_key()
        key_hash = hash_key(key)

        def q(conn):
            with conn.transaction():
                org = conn.execute(
                    'select id from organizations where name = %s',
                    (org_name,)).fetchone()
                if org is None:
                    org = conn.execute(
                        'insert into organizations (name) values (%s)'
                        ' returning id', (org_name,)).fetchone()
                conn.execute(
                    'insert into api_keys (org_id, key_hash, label)'
                    ' values (%s, %s, %s)', (org[0], key_hash, label))
                if credits and credits > 0:
                    conn.execute(
                        "insert into credit_ledger (key_id, amount, kind,"
                        " idempotency_key, reference_id)"
                        " select id, %s, 'topup', 'issue:' || %s, null"
                        " from api_keys where key_hash = %s",
                        (float(credits), _uid(), key_hash))
        self._call(q)
        record = self.lookup(key_hash) or {}
        return key, record

    def revoke(self, key_hash: str) -> bool:
        def q(conn):
            cur = conn.execute(
                "update api_keys set status = 'revoked', revoked_at = now()"
                " where key_hash = %s and status = 'active'", (key_hash,))
            return cur.rowcount > 0
        return self._call(q)

    def add_credits(self, key_hash: str, credits: float) -> bool:
        """Ajustement manuel (CLI, opérateur). Un retrait est plafonné au
        solde disponible : jamais de solde négatif."""
        if not credits:
            return self.balance(key_hash) is not None
        amount = float(credits)
        key_id = self._key_id(key_hash)
        if key_id is None:
            return False

        def q(conn):
            if amount > 0:
                conn.execute(
                    "insert into credit_ledger (key_id, amount, kind,"
                    " idempotency_key, reference_id)"
                    " values (%s, %s, 'adjustment', %s, null)",
                    (key_id, amount, 'adjustment:' + _uid()))
            else:
                bal = conn.execute(
                    'select coalesce(credits_balance, 0)'
                    ' from api_key_balances where key_id = %s',
                    (key_id,)).fetchone()
                take = min(-amount, max(float(bal[0]), 0.0)) if bal else 0.0
                if take > 0:
                    conn.execute(
                        "insert into credit_ledger (key_id, amount, kind,"
                        " idempotency_key, reference_id)"
                        " values (%s, %s, 'adjustment', %s, null)",
                        (key_id, -take, 'adjustment:' + _uid()))
        self._call(q)
        return True

    # opérations financières : fonctions SQL transactionnelles
    def reserve(self, key_hash: str, credits: float = 1.0,
                job_id: str = '') -> bool:
        """Réserve atomiquement des crédits (idempotente par job)."""
        def q(conn):
            row = conn.execute(
                'select reserve_credits(%s::char(64), %s::numeric, %s)',
                (key_hash, float(credits), job_id or ('cli-' + _uid()))
            ).fetchone()
            return bool(row and row[0])
        return self._call(q)

    def settle(self, key_hash: str, extra: float,
               job_id: str = '') -> float:
        """Complète une réservation : débite max(0, coût_total − réservé),
        plafonné au solde disponible. `extra` = coût réel − réservation."""
        if not job_id:
            return self._settle_standalone(key_hash, float(extra))

        def q(conn):
            # réservation réelle du job (lue DANS la transaction)
            res = conn.execute(
                "select -coalesce(sum(amount), 0) from credit_ledger l"
                " join api_keys k on k.id = l.key_id"
                " where k.key_hash = %s and l.kind = 'reserve'"
                " and l.idempotency_key = 'job:' || %s",
                (key_hash, job_id)).fetchone()
            total = max(float(res[0] or 0) + float(extra), 0)
            row = conn.execute(
                'select settle_credits(%s::char(64), %s::numeric, %s, true)',
                (key_hash, total, job_id)).fetchone()
            return float(row[0]) if row and row[0] is not None else -1.0
        return self._call(q)

    def refund(self, key_hash: str, credits: float,
               job_id: str = '') -> bool:
        """Restitue la réservation d'un job (échec → rien consommé).
        Via la fonction SQL : reverse exactement la réservation du job ;
        le montant passé est ignoré (source de vérité : le grand livre)."""
        if not job_id:
            # hors flux API : assimilé à un ajustement positif
            return self.add_credits(key_hash, abs(float(credits)))
        self._settle_failed(key_hash, job_id)
        return True

    def _settle_failed(self, key_hash: str, job_id: str):
        def q(conn):
            conn.execute('select settle_credits(%s::char(64), %s::numeric, %s, false)',
                         (key_hash, 0, job_id))
        self._call(q)

    def _settle_standalone(self, key_hash: str, extra: float) -> float:
        """Décompte hors flux API (CLI/réconciliation) : ajustement plafonné."""
        if extra <= 0:
            return self.balance(key_hash) or -1.0
        key_id = self._key_id(key_hash)
        if key_id is None:
            return -1.0

        def q(conn):
            bal = conn.execute(
                'select coalesce(credits_balance, 0) from api_key_balances'
                ' where key_id = %s', (key_id,)).fetchone()
            take = min(extra, max(float(bal[0]), 0.0)) if bal else 0.0
            if take > 0:
                conn.execute(
                    "insert into credit_ledger (key_id, amount, kind,"
                    " idempotency_key, reference_id)"
                    " values (%s, %s, 'settle', %s, null)",
                    (key_id, -take, 'settle:' + _uid()))
            row = conn.execute(
                'select credits_balance from api_key_balances'
                ' where key_id = %s', (key_id,)).fetchone()
            return float(row[0]) if row else -1.0
        return self._call(q)

    def list(self) -> list[dict]:
        def q(conn):
            rows = conn.execute(
                'select k.label, k.key_hash, k.status, k.created_at,'
                ' coalesce(b.credits_balance, 0) from api_keys k'
                ' left join api_key_balances b on b.key_id = k.id'
                ' order by k.created_at').fetchall()
            return [{'label': r[0], 'key_hash': r[1], 'credits': float(r[4]),
                     'revoked': r[2] != 'active', 'created': _iso(r[3])}
                    for r in rows]
        return self._call(q)

    def _key_id(self, key_hash: str) -> Optional[str]:
        def q(conn):
            row = conn.execute(
                'select id::text from api_keys where key_hash = %s',
                (key_hash,)).fetchone()
            return row[0] if row else None
        return self._call(q)

    # ── interface BillingStore ────────────────────────────────────
    def create_topup(self, key_hash: str, pack: str, session: dict) -> dict:
        from .billing import CREDIT_PACKS
        ref = 'TOPUP-' + uuid.uuid4().hex[:12].upper()

        def q(conn):
            key_id = conn.execute(
                'select id from api_keys where key_hash = %s',
                (key_hash,)).fetchone()
            if key_id is None:
                raise PgStoreError('Clé inconnue pour le rechargement.')
            conn.execute(
                'insert into topups (reference, key_id, pack, credits,'
                ' amount_fcfa, session_id, checkout_url)'
                ' values (%s, %s, %s, %s, %s, %s, %s)',
                (ref, key_id[0], pack, CREDIT_PACKS[pack]['credits'],
                 CREDIT_PACKS[pack]['amount_fcfa'], session.get('id', ''),
                 session.get('checkout_url', '')))
        self._call(q)
        return self.get(ref) or {}

    def get(self, ref: str) -> Optional[dict]:
        def q(conn):
            row = conn.execute(
                'select t.reference, t.session_id, t.checkout_url, k.key_hash,'
                ' t.pack, t.credits, t.amount_fcfa, t.status,'
                ' t.transaction_id, t.created_at, t.activated_at'
                ' from topups t join api_keys k on k.id = t.key_id'
                ' where t.reference = %s', (ref,)).fetchone()
            return None if row is None else _topup_row_to_dict(row)
        return self._call(q)

    def pending(self) -> list[dict]:
        def q(conn):
            rows = conn.execute(
                'select t.reference, t.session_id, t.checkout_url, k.key_hash,'
                ' t.pack, t.credits, t.amount_fcfa, t.status,'
                ' t.transaction_id, t.created_at, t.activated_at'
                ' from topups t join api_keys k on k.id = t.key_id'
                " where t.status = 'pending' order by t.created_at").fetchall()
            return [_topup_row_to_dict(r) for r in rows]
        return self._call(q)

    def activate(self, ref: str, transaction_id: str) -> Optional[dict]:
        """Activation TRANSACTIONNELLE (fonction SQL activate_topup) :
        marque le rechargement ET crédite le grand livre dans UNE
        transaction. Idempotence triple (statut, transaction unique,
        grand livre unique). ⚠️ Crédite déjà les crédits : l'appelant ne
        doit PAS appeler add_credits ensuite (COMBINED_CREDIT_ACTIVATION)."""
        def q(conn):
            row = conn.execute('select activate_topup(%s, %s)',
                               (ref, transaction_id)).fetchone()
            return bool(row and row[0])
        ok = self._call(q)
        return self.get(ref) if ok else None

    def mark_failed(self, ref: str, transaction_id: str = ''):
        def q(conn):
            conn.execute('select fail_topup(%s, %s)',
                         (ref, transaction_id or ''))
        self._call(q)

    def mark_refunded(self, ref: str) -> Optional[dict]:
        def q(conn):
            cur = conn.execute(
                "update topups set status = 'refunded' where reference = %s"
                " and status = 'activated'", (ref,))
            return cur.rowcount > 0
        ok = self._call(q)
        return self.get(ref) if ok else None

    def webhook_seen(self, event_key: str) -> bool:
        def q(conn):
            return conn.execute(
                'select 1 from webhook_events where event_key = %s',
                (event_key,)).fetchone() is not None
        return self._call(q)

    def mark_webhook(self, event_key: str, payload: Optional[dict] = None):
        """Enregistre un événement webhook (idempotent par event_key)."""
        def q(conn):
            row = conn.execute(
                'select record_webhook_event(%s, %s::jsonb)',
                (event_key, json.dumps(payload or {}))).fetchone()
            return bool(row and row[0])
        return self._call(q)

    # ── usage (record_usage + usage) ──────────────────────────────
    def record_usage(self, key_hash: str, pages: int, transactions: int):
        def q(conn):
            conn.execute('select record_usage(%s::char(64), %s, %s)',
                         (key_hash, int(pages), int(transactions)))
        self._call(q)

    def usage(self, key_hash: str) -> dict:
        def q(conn):
            row = conn.execute(
                'select u.documents, u.pages, u.transactions'
                ' from usage_counters u join api_keys k on k.id = u.key_id'
                ' where k.key_hash = %s', (key_hash,)).fetchone()
            if row is None:
                return {'documents': 0, 'pages': 0, 'transactions': 0}
            return {'documents': row[0], 'pages': row[1],
                    'transactions': row[2]}
        return self._call(q)

    def usage_snapshot(self) -> dict[str, dict]:
        """Vue complète des compteurs (hash → compteurs), route admin."""
        def q(conn):
            rows = conn.execute(
                'select k.key_hash, u.documents, u.pages, u.transactions'
                ' from usage_counters u join api_keys k on k.id = u.key_id'
                ' order by k.key_hash').fetchall()
            return {r[0]: {'documents': r[1], 'pages': r[2],
                           'transactions': r[3]} for r in rows}
        return self._call(q)

    def usage_reset(self, key_hash: str):
        def q(conn):
            conn.execute(
                'delete from usage_counters where key_id in'
                ' (select id from api_keys where key_hash = %s)',
                (key_hash,))
        self._call(q)

    # ── jobs (métadonnées de traçabilité ; jamais le résultat bancaire) ──
    def record_job(self, job_id: str, key_hash: str, filename: str,
                   kind: str, status: str, pages: Optional[int] = None,
                   rows: Optional[int] = None,
                   transactions: Optional[int] = None,
                   credits_charged: Optional[float] = None,
                   error: Optional[str] = None, ttl_s: int = 900) -> bool:
        """Journalise/metà-jour un job. La politique « traitement éphémère »
        est conservée : seules les métadonnées sont persistées, jamais les
        transactions bancaires du résultat. Renvoie False pour une clé non
        gérée (rien à journaliser)."""
        key_id = self._key_id(key_hash)
        if key_id is None:
            return False

        def q(conn):
            conn.execute(
                "insert into jobs (id, key_id, filename, kind, status,"
                " pages, rows, transactions_count, credits_charged, error,"
                " expires_at)"
                " values (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                " now() + (%s || ' seconds')::interval)"
                " on conflict (id) do update set status = excluded.status,"
                " pages = excluded.pages, rows = excluded.rows,"
                " transactions_count = excluded.transactions_count,"
                " credits_charged = excluded.credits_charged,"
                " error = excluded.error",
                (job_id, key_id, filename, kind, status, pages, rows,
                 transactions, credits_charged, error, int(ttl_s)))
            return True
        return self._call(q)

    # ── maintenance / réconciliation (cron) ───────────────────────
    def purge_expired_jobs(self) -> int:
        def q(conn):
            return conn.execute(
                'delete from jobs where expires_at < now()').rowcount
        return self._call(q)

    def purge_old_webhook_events(self, days: int = 30) -> int:
        def q(conn):
            return conn.execute(
                'delete from webhook_events'
                " where received_at < now() - (%s || ' days')::interval",
                (int(days),)).rowcount
        return self._call(q)

    def reconcile_topups(self) -> list[dict]:
        """Rechargements ACTIVÉS dont le crédit n'apparaît pas dans le
        grand livre : incohérence à signaler (jamais corrigée seule)."""
        def q(conn):
            rows = conn.execute(
                "select t.reference, t.pack, t.credits, t.amount_fcfa,"
                " t.activated_at, k.key_hash from topups t"
                " join api_keys k on k.id = t.key_id"
                " where t.status = 'activated' and not exists ("
                "   select 1 from credit_ledger l"
                "   where l.key_id = t.key_id"
                "     and l.idempotency_key = 'topup:' || t.reference)"
            ).fetchall()
            return [{'reference': r[0], 'pack': r[1], 'credits': r[2],
                     'amount_fcfa': r[3], 'activated_at': _iso(r[4]),
                     'key_hash': r[5]} for r in rows]
        return self._call(q)

    def orphan_credits(self) -> list[dict]:
        """Mouvements 'topup' du grand livre sans rechargement activé
        correspondant (crédit attribué sans paiement valide)."""
        def q(conn):
            rows = conn.execute(
                "select l.reference_id, l.key_id::text, l.amount,"
                " l.created_at, k.key_hash from credit_ledger l"
                " join api_keys k on k.id = l.key_id"
                " where l.kind = 'topup'"
                " and l.idempotency_key like 'topup:%'"
                " and not exists (select 1 from topups t"
                "   where t.status in ('activated', 'refunded')"
                "     and 'topup:' || t.reference = l.idempotency_key)"
            ).fetchall()
            return [{'reference_id': r[0], 'key_id': r[1],
                     'amount': float(r[2]), 'created_at': _iso(r[3]),
                     'key_hash': r[4]} for r in rows]
        return self._call(q)

    def negative_balances(self) -> list[dict]:
        """Soldes incohérents (négatifs) — théoriquement impossibles."""
        def q(conn):
            rows = conn.execute(
                'select key_hash, credits_balance from api_key_balances'
                ' where credits_balance < 0').fetchall()
            return [{'key_hash': r[0], 'balance': float(r[1])}
                    for r in rows]
        return self._call(q)

    def audit(self, event: str, detail: Optional[dict] = None,
              key_hash: str = ''):
        key_id = self._key_id(key_hash) if key_hash else None

        def q(conn):
            conn.execute(
                'insert into audit_log (key_id, event, detail)'
                ' values (%s, %s, %s::jsonb)',
                (key_id, event, json.dumps(detail or {})))
        self._call(q)

    def audit_recent(self, limit: int = 50) -> list[dict]:
        def q(conn):
            rows = conn.execute(
                'select event, detail, created_at from audit_log'
                ' order by created_at desc limit %s', (int(limit),)).fetchall()
            return [{'event': r[0], 'detail': r[1], 'created_at': _iso(r[2])}
                    for r in rows]
        return self._call(q)

    def expired_pending_topups(self, older_than_hours: int = 24) -> list[dict]:
        """Rechargements pending plus vieux que N heures : à vérifier chez
        la passerelle lors de la réconciliation."""
        def q(conn):
            rows = conn.execute(
                'select t.reference, t.session_id, k.key_hash, t.created_at'
                ' from topups t join api_keys k on k.id = t.key_id'
                " where t.status = 'pending'"
                "   and t.created_at < now() - (%s || ' hours')::interval",
                (int(older_than_hours),)).fetchall()
            return [{'reference': r[0], 'session_id': r[1],
                     'key_hash': r[2], 'created_at': _iso(r[3])}
                    for r in rows]
        return self._call(q)


def _topup_row_to_dict(r) -> dict:
    return {
        'reference': r[0], 'session_id': r[1], 'checkout_url': r[2],
        'key_hash': r[3], 'pack': r[4], 'credits': r[5],
        'amount_fcfa': r[6], 'status': r[7], 'transaction_id': r[8],
        'created': _iso(r[9]), 'activated_at': _iso(r[10]),
    }


class PgUsageStore:
    """Adaptateur UsageStore ↔ PgStore (get/record/snapshot/reset) :
    l'API permutte usage JSON → PostgreSQL sans changer ses appels."""

    def __init__(self, store: PgStore):
        self._store = store

    def get(self, key_hash: str) -> dict:
        return self._store.usage(key_hash)

    def record(self, key_hash: str, pages: int, transactions: int):
        self._store.record_usage(key_hash, pages, transactions)

    def snapshot(self) -> dict[str, dict]:
        return self._store.usage_snapshot()

    def reset(self, key_hash: str):
        self._store.usage_reset(key_hash)


def pgstore_from_env() -> tuple[Optional[PgStore], str]:
    """(store, mode) selon l'environnement : PgStore si DATABASE_URL est
    défini, sinon (None, 'json') — l'appelant garde ses magasins JSON."""
    dsn = os.environ.get(ENV_PREFIX + 'DATABASE_URL', '').strip()
    if not dsn:
        return None, 'json'

    def _int_env(name: str, default: int) -> int:
        try:
            return int(os.environ.get(ENV_PREFIX + name, default))
        except (TypeError, ValueError):
            return default

    return PgStore(dsn, pool_min=_int_env('PG_POOL_MIN', 0),
                   pool_max=_int_env('PG_POOL_MAX', 4)), 'postgres'
