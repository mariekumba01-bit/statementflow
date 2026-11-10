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
import unicodedata
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

# Catégories Unicode retirées d'un DSN : Cf (marques bidi U+200E/U+200F,
# BOM U+FEFF, zero-width U+200B-U+200D, LRE/LRI…) et Cc (contrôles).
# str.strip() NE LES RETIRE PAS (isspace() == False) et libpq refuse alors
# l'URI (« missing "=" after "postgresql://…" in connection info string »
# — incident Render du 2026-11-08 : un U+200E copié-collé devant la
# chaîne de connexion Supabase).
_INVISIBLE_CATS = frozenset({'Cf', 'Cc'})


class PgStoreError(RuntimeError):
    """Erreur de persistance PostgreSQL (message sans secret)."""


def sanitize_dsn(raw: str) -> str:
    """Nettoie une URL de connexion PostgreSQL : retire les caractères
    Unicode invisibles (copier-coller depuis une interface web ou un
    traitement de texte) puis les espaces en tête/queue. Le reste de la
    chaîne est préservé à l'identique."""
    if not raw:
        return raw
    cleaned = ''.join(
        ch for ch in raw if unicodedata.category(ch) not in _INVISIBLE_CATS)
    return cleaned.strip()


def classify_pg_error(e: BaseException) -> str:
    """Type d'échec de connexion pour /health (diagnostic SANS secret :
    les messages libpq de parsing contiennent la chaîne complète, mot de
    passe compris — ils ne doivent jamais être exposés ni journalisés)."""
    msg = str(e).lower()
    connish = isinstance(e, psycopg.OperationalError) \
        or isinstance(e, PoolTimeout) or 'connection' in msg
    if not connish:
        return 'unknown'
    if ('missing' in msg and '=' in msg) or 'connection info' in msg \
            or 'invalid connection' in msg:
        return 'dsn_invalid'
    if 'password authentication' in msg or 'authentication failed' in msg:
        return 'auth_failed'
    if 'ssl' in msg or 'tls' in msg or 'certificate' in msg:
        return 'tls_error'
    return 'unreachable'


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

    def __init__(self, dsn: str, pool_min: int = 0, pool_max: int = 4,
                 pool_timeout: float = POOL_TIMEOUT_S):
        if psycopg is None:
            raise PgStoreError(
                'psycopg non installé : pip install "psycopg[binary,pool]"')
        # le DSN est nettoyé UNE fois ici : toute clé gérée, tout appel
        # financier et tout diagnostic utilisent ensuite la version saine
        self._dsn = sanitize_dsn(dsn)
        self._pool_min, self._pool_max = int(pool_min), int(pool_max)
        self._pool_timeout = float(pool_timeout)
        self._pool_lock = threading.Lock()
        self._pool: Optional['ConnectionPool'] = None
        # dernier type d'échec de connexion (diagnostic /health —
        # 'dsn_invalid' | 'auth_failed' | 'tls_error' | 'unreachable' |
        # 'unknown' ; JAMAIS le message brut, qui peut porter le secret)
        self.last_error_kind: Optional[str] = None

    @property
    def dsn(self) -> str:
        """DSN de connexion nettoyé (pour les connexions dédiées —
        verrou cron)."""
        return self._dsn

    # ── pool de connexions ────────────────────────────────────────
    def _validate_dsn(self) -> dict:
        """Valide le DSN (parsing libpq) AVANT toute tentative de
        connexion : le message d'erreur de parsing contient la chaîne
        COMPLÈTE (mot de passe compris) — il ne doit jamais atteindre un
        journal. Retourne les paramètres de connexion parsés."""
        try:
            return psycopg.conninfo.conninfo_to_dict(self._dsn)
        except Exception:
            self.last_error_kind = 'dsn_invalid'
            raise PgStoreError(
                'DSN PostgreSQL invalide (STATEMENTFLOW_DATABASE_URL) : '
                'format attendu postgresql://utilisateur:motdepasse@'
                'hote:port/base — les caractères invisibles de '
                'copier-coller sont nettoyés automatiquement, mais le '
                'format doit rester correct') from None

    @staticmethod
    def _pool_kwargs(params: dict) -> dict:
        """Arguments psycopg selon le DSN : le pooler Supabase en mode
        TRANSACTION (port 6543, pgBouncer) n'accepte pas les requêtes
        préparées persistantes — psycopg3 les active par défaut et doit
        les voir désactivées. Le mode session (5432) les supporte."""
        if str(params.get('port') or '') == '6543':
            return {'prepare_threshold': None}
        return {}

    @property
    def pool(self) -> 'ConnectionPool':
        pool = self._pool
        if pool is not None and not pool.closed:
            return pool
        with self._pool_lock:
            if self._pool is None or self._pool.closed:
                params = self._validate_dsn()
                try:
                    self._pool = ConnectionPool(
                        self._dsn, min_size=self._pool_min,
                        max_size=self._pool_max,
                        timeout=self._pool_timeout, open=True,
                        kwargs=self._pool_kwargs(params),
                        name='statementflow')
                except Exception as e:
                    self._pool = None
                    self._note_error(e)
                    raise PgStoreError(
                        f'PostgreSQL injoignable ({_mask_dsn(self._dsn)}) : '
                        f'{self.last_error_kind}') from None
            return self._pool

    def _note_error(self, e: BaseException) -> None:
        self.last_error_kind = classify_pg_error(e)

    def _call(self, fn):
        """Exécute fn(conn) sur une connexion du pool ; UNE relance si la
        connexion était périmée (les mutations SQL étant idempotentes, un
        rejeu après coupure ne double jamais un mouvement financier).
        Un échec de CONNEXION est converti en PgStoreError classée : le
        message psycopg brut ne remonte jamais (risque de secret)."""
        try:
            with self.pool.connection() as conn:
                return fn(conn)
        except (PoolTimeout, psycopg.OperationalError) as e:
            self._note_error(e)
            self._reset_pool()
            try:
                with self.pool.connection() as conn:
                    return fn(conn)
            except (PoolTimeout, psycopg.OperationalError) as e2:
                self._note_error(e2)
                raise PgStoreError(
                    f'PostgreSQL indisponible ({_mask_dsn(self._dsn)}) : '
                    f'{self.last_error_kind}') from None

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
        """Sonde la base (true/false) — /health et cron. En cas d'échec,
        last_error_kind porte le TYPE d'erreur (jamais le message)."""
        try:
            self._call(lambda c: c.execute('select 1'))
            self.last_error_kind = None
            return True
        except PgStoreError:
            # déjà classifiée (dsn_invalid / unreachable / …) par le
            # chemin qui l'a levée — ne pas écraser par 'unknown'
            return False
        except Exception as e:
            self._note_error(e)
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

    # ── jobs (métadonnées + résultat TTL — v0.6.1) ────────────────────
    def record_job(self, job_id: str, key_hash: str, filename: str,
                   kind: str, status: str, pages: Optional[int] = None,
                   rows: Optional[int] = None,
                   transactions: Optional[int] = None,
                   credits_charged: Optional[float] = None,
                   error: Optional[str] = None, ttl_s: int = 900,
                   result: Optional[dict] = None) -> bool:
        """Journalise/metà-jour un job. Les MÉTADONNÉES (nom, pages,
        transactions, crédits) alimentent l'historique durable du
        compte ; `result` (résultat complet, jobs done uniquement) est
        couvert par le MÊME TTL expires_at — purgé à l'expiration par
        purge_expired_jobs : la politique « traitement éphémère » est
        inchangée, le résultat survit simplement aux redémarrages
        PENDANT sa période de rétention. Renvoie False pour une clé
        non gérée (rien à journaliser)."""
        key_id = self._key_id(key_hash)
        if key_id is None:
            return False

        def q(conn):
            conn.execute(
                "insert into jobs (id, key_id, filename, kind, status,"
                " pages, rows, transactions_count, credits_charged, error,"
                " expires_at, result)"
                " values (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                " now() + (%s || ' seconds')::interval, %s::jsonb)"
                " on conflict (id) do update set status = excluded.status,"
                " pages = excluded.pages, rows = excluded.rows,"
                " transactions_count = excluded.transactions_count,"
                " credits_charged = excluded.credits_charged,"
                " error = excluded.error, result = excluded.result",
                (job_id, key_id, filename, kind, status, pages, rows,
                 transactions, credits_charged, error, int(ttl_s),
                 json.dumps(result) if result is not None else None))
            return True
        return self._call(q)

    def get_job_for_key(self, key_hash: str,
                        job_id: str) -> Optional[dict]:
        """Job (statut, résultat, erreur) pour la clé PROPRIÉTAIRE
        uniquement, tant que expires_at > now() (TTL). None si :
        job inconnu, expiré, appartenant à une autre clé — les trois
        cas sont indistinguables pour l'appelant (pas de sonde
        d'existence). Le résultat n'est renvoyé que pour les jobs done
        (colonne result, NULL pour les jobs antérieurs à la v0.6.1)."""
        def q(conn):
            row = conn.execute(
                'select status, result, error, expires_s'
                ' from get_job_for_key(%s, %s::uuid)',
                (key_hash, job_id)).fetchone()
            if row is None:
                return None
            return {'status': row[0],
                    'result': row[1],
                    'error': row[2],
                    'expires_s': row[3]}
        return self._call(q)

    def get_job_for_user(self, user_id: str,
                         job_id: str) -> Optional[dict]:
        """Variante par COMPTE (session web) : toutes les clés du compte
        (active ou révoquée par rotation) peuvent consulter le job,
        comme user_owns_key_hash en mode mémoire. Mêmes règles de TTL
        et d'indistinguabilité que get_job_for_key."""
        def q(conn):
            row = conn.execute(
                'select j.status, j.result, j.error,'
                ' greatest(extract(epoch from (j.expires_at - now())), 0)'
                '::float8 as expires_s'
                ' from jobs j join api_keys k on k.id = j.key_id'
                ' where k.user_id = %s::uuid and j.id = %s::uuid'
                ' and j.expires_at > now()',
                (user_id, job_id)).fetchone()
            if row is None:
                return None
            return {'status': row[0], 'result': row[1], 'error': row[2],
                    'expires_s': row[3]}
        return self._call(q)

    # ── comptes clients (v0.5.0 — migration 005) ──────────────────
    def create_account(self, email: str, password_hash: str,
                       password_salt: str, key_hash: str,
                       bonus_credits: float = 0.0, verified: bool = False,
                       verified_via: Optional[str] = None,
                       source: str = 'direct',
                       ref: Optional[str] = None) -> str:
        """Compte complet (organisation + utilisateur + clé API + bonus
        éventuel) en UNE transaction SQL. Lève PgStoreError
        'email_deja_utilise' si l'email existe déjà."""
        def q(conn):
            return conn.execute(
                'select create_account(%s, %s, %s, %s, %s::numeric,'
                ' %s, %s, %s, %s)',
                (email, password_hash, password_salt, key_hash,
                 bonus_credits, verified, verified_via, source,
                 ref)).fetchone()[0]
        try:
            return self._call(q)
        except Exception as e:
            if 'email_deja_utilise' in str(e):
                raise PgStoreError('email_deja_utilise') from None
            raise

    def get_user_by_email(self, email: str) -> Optional[dict]:
        def q(conn):
            row = conn.execute(
                'select id, email, password_hash, password_salt,'
                ' email_verified, verified_via, created_at, last_login_at'
                ' from users where email = %s', (email,)).fetchone()
            return None if row is None else {
                'id': str(row[0]), 'email': row[1], 'password_hash': row[2],
                'password_salt': row[3], 'email_verified': row[4],
                'verified_via': row[5], 'created': _iso(row[6]),
                'last_login': _iso(row[7])}
        return self._call(q)

    def mark_email_verified(self, user_id: str, via: str = 'smtp') -> bool:
        def q(conn):
            return conn.execute(
                'update users set email_verified = true, verified_via = %s'
                ' where id = %s::uuid', (via, user_id)).rowcount > 0
        return self._call(q)

    def touch_login(self, user_id: str) -> None:
        def q(conn):
            conn.execute('update users set last_login_at = now()'
                         ' where id = %s::uuid', (user_id,))
        self._call(q)

    def create_session(self, user_id: str, token_hash: str,
                       ttl_s: int) -> None:
        def q(conn):
            conn.execute(
                'insert into sessions (user_id, token_hash, expires_at)'
                ' values (%s::uuid, %s, now() + (%s || \' seconds\')'
                '::interval)', (user_id, token_hash, int(ttl_s)))
        self._call(q)

    def get_session_user(self, token_hash: str) -> Optional[dict]:
        """Utilisateur de la session (ou None : inexistante/expirée).
        Rafraîchit last_seen_at (meilleur effort)."""
        def q(conn):
            row = conn.execute(
                'select u.id, u.email, u.email_verified, u.verified_via'
                ' from sessions s join users u on u.id = s.user_id'
                ' where s.token_hash = %s and s.expires_at > now()',
                (token_hash,)).fetchone()
            if row is not None:
                conn.execute('update sessions set last_seen_at = now()'
                             ' where token_hash = %s', (token_hash,))
            return None if row is None else {
                'id': str(row[0]), 'email': row[1],
                'email_verified': row[2], 'verified_via': row[3]}
        return self._call(q)

    def delete_session(self, token_hash: str) -> None:
        def q(conn):
            conn.execute('delete from sessions where token_hash = %s',
                         (token_hash,))
        self._call(q)

    def create_auth_token(self, user_id: str, purpose: str,
                          token_hash: str, ttl_s: int) -> None:
        def q(conn):
            conn.execute(
                'insert into auth_tokens (user_id, purpose, token_hash,'
                ' expires_at) values (%s::uuid, %s, %s,'
                ' now() + (%s || \' seconds\')::interval)',
                (user_id, purpose, token_hash, int(ttl_s)))
        self._call(q)

    def consume_auth_token(self, purpose: str,
                           token_hash: str) -> Optional[str]:
        """Consomme le token (usage UNIQUE, atomique) → user_id ou None."""
        def q(conn):
            row = conn.execute(
                'update auth_tokens set used_at = now()'
                ' where token_hash = %s and purpose = %s'
                ' and used_at is null and expires_at > now()'
                ' returning user_id', (token_hash, purpose)).fetchone()
            return None if row is None else str(row[0])
        return self._call(q)

    def user_active_key_hash(self, user_id: str) -> Optional[str]:
        """Hash de la clé ACTIVE du compte (une seule par compte)."""
        def q(conn):
            row = conn.execute(
                'select key_hash from api_keys'
                ' where user_id = %s::uuid and status = \'active\''
                ' order by created_at desc limit 1', (user_id,)).fetchone()
            return None if row is None else row[0]
        return self._call(q)

    def user_owns_key_hash(self, user_id: str, key_hash: str) -> bool:
        """True si la clé (active OU révoquée par rotation) appartient au
        compte — l'historique reste accessible après rotation."""
        def q(conn):
            return conn.execute(
                'select exists(select 1 from api_keys'
                ' where user_id = %s::uuid and key_hash = %s)',
                (user_id, key_hash)).fetchone()[0]
        return self._call(q)

    def list_jobs_for_user(self, user_id: str, limit: int = 20,
                           offset: int = 0) -> list[dict]:
        """Historique des conversions du compte, toutes clés confondues
        (MÉTADONNÉES — jamais les transactions bancaires)."""
        def q(conn):
            rows = conn.execute(
                'select j.id, j.filename, j.kind, j.status, j.pages,'
                ' j.rows, j.transactions_count, j.credits_charged,'
                ' j.error, j.created_at from jobs j'
                ' join api_keys k on k.id = j.key_id'
                ' where k.user_id = %s::uuid'
                ' order by j.created_at desc limit %s offset %s',
                (user_id, int(limit), int(offset))).fetchall()
            return [{'job_id': str(r[0]).replace('-', ''),
                     'filename': r[1], 'kind': r[2],
                     'status': r[3], 'pages': r[4], 'rows': r[5],
                     'transactions': r[6],
                     'credits_charged': float(r[7]) if r[7] else None,
                     'error': r[8], 'created_at': _iso(r[9])}
                    for r in rows]
        return self._call(q)

    def list_topups_for_user(self, user_id: str, limit: int = 20
                             ) -> list[dict]:
        """Historique d'achats du compte, toutes clés confondues."""
        def q(conn):
            rows = conn.execute(
                'select t.reference, t.pack, t.credits, t.amount_fcfa,'
                ' t.status, t.transaction_id, t.created_at, t.activated_at'
                ' from topups t join api_keys k on k.id = t.key_id'
                ' where k.user_id = %s::uuid'
                ' order by t.created_at desc limit %s',
                (user_id, int(limit))).fetchall()
            return [{'reference': r[0], 'pack': r[1], 'credits': r[2],
                     'amount_fcfa': r[3], 'status': r[4],
                     'transaction_id': r[5], 'created_at': _iso(r[6]),
                     'activated_at': _iso(r[7])} for r in rows]
        return self._call(q)

    def get_topup_for_user(self, reference: str,
                           user_id: str) -> Optional[dict]:
        """Rechargement du compte (toutes clés confondues) ou None."""
        def q(conn):
            row = conn.execute(
                'select t.reference, t.key_id, k.key_hash, t.pack,'
                ' t.credits, t.amount_fcfa, t.session_id, t.status,'
                ' t.transaction_id from topups t'
                ' join api_keys k on k.id = t.key_id'
                ' where t.reference = %s and k.user_id = %s::uuid',
                (reference, user_id)).fetchone()
            return None if row is None else {
                'reference': row[0], 'key_id': str(row[1]),
                'key_hash': row[2], 'pack': row[3], 'credits': row[4],
                'amount_fcfa': row[5], 'session_id': row[6],
                'status': row[7], 'transaction_id': row[8]}
        return self._call(q)

    def rotate_key(self, user_id: str, new_key_hash: str) -> float:
        """Rotation : révoque l'ancienne clé, crée la nouvelle, transfère
        le solde (fonction SQL transactionnelle). Retourne le solde
        transféré."""
        def q(conn):
            return float(conn.execute(
                'select rotate_user_key(%s::uuid, %s)',
                (user_id, new_key_hash)).fetchone()[0])
        return self._call(q)

    def record_event(self, type_: str, anonymous_id: str = '',
                     user_id: Optional[str] = None, source: str = 'direct',
                     path: str = '', metadata: Optional[dict] = None) -> None:
        """Événement produit/marketing (aucune donnée bancaire).
        Best effort : jamais bloquant pour l'appelant."""
        def q(conn):
            conn.execute(
                'insert into events (type, anonymous_id, user_id, source,'
                ' path, metadata) values (%s, %s, %s::uuid, %s, %s, %s)',
                (type_, (anonymous_id or '')[:120], user_id,
                 (source or 'direct')[:60], (path or '')[:200],
                 json.dumps(metadata or {})))
        try:
            self._call(q)
        except Exception:
            pass

    def list_jobs_for_key(self, key_hash: str, limit: int = 20,
                          offset: int = 0) -> list[dict]:
        """Historique des conversions d'une clé (MÉTADONNÉES uniquement —
        jamais les transactions bancaires du résultat)."""
        def q(conn):
            rows = conn.execute(
                'select j.id, j.filename, j.kind, j.status, j.pages,'
                ' j.rows, j.transactions_count, j.credits_charged,'
                ' j.error, j.created_at from jobs j'
                ' join api_keys k on k.id = j.key_id'
                ' where k.key_hash = %s'
                ' order by j.created_at desc limit %s offset %s',
                (key_hash, int(limit), int(offset))).fetchall()
            return [{'job_id': str(r[0]).replace('-', ''),
                     'filename': r[1], 'kind': r[2],
                     'status': r[3], 'pages': r[4], 'rows': r[5],
                     'transactions': r[6],
                     'credits_charged': float(r[7]) if r[7] else None,
                     'error': r[8], 'created_at': _iso(r[9])}
                    for r in rows]
        return self._call(q)

    def list_topups_for_key(self, key_hash: str, limit: int = 20
                            ) -> list[dict]:
        def q(conn):
            rows = conn.execute(
                'select t.reference, t.pack, t.credits, t.amount_fcfa,'
                ' t.status, t.transaction_id, t.created_at, t.activated_at'
                ' from topups t join api_keys k on k.id = t.key_id'
                ' where k.key_hash = %s'
                ' order by t.created_at desc limit %s',
                (key_hash, int(limit))).fetchall()
            return [{'reference': r[0], 'pack': r[1], 'credits': r[2],
                     'amount_fcfa': r[3], 'status': r[4],
                     'transaction_id': r[5], 'created_at': _iso(r[6]),
                     'activated_at': _iso(r[7])} for r in rows]
        return self._call(q)

    def admin_stats(self) -> dict:
        """Statistiques commerciales agrégées. Le chiffre d'affaires ne
        compte QUE les rechargements 'activated' (jamais les pending)."""
        def q(conn):
            row = conn.execute(
                "select"
                " (select count(distinct anonymous_id) from events"
                "   where type = 'visit'),"
                " (select count(*) from users),"
                " (select count(*) from jobs where status = 'done'),"
                " (select coalesce(sum(credits_charged), 0) from jobs"
                "   where status = 'done'),"
                " (select count(*) from topups where status = 'activated'),"
                " (select coalesce(sum(amount_fcfa), 0) from topups"
                "   where status = 'activated'),"
                " (select count(distinct k.user_id) from jobs j"
                "   join api_keys k on k.id = j.key_id"
                "   where j.status = 'done' and k.user_id is not null),"
                " (select count(distinct t.key_id) from topups t"
                "   where t.status = 'activated')").fetchone()
            packs = conn.execute(
                "select pack, count(*), coalesce(sum(amount_fcfa), 0),"
                " coalesce(sum(credits), 0) from topups"
                " where status = 'activated' group by pack"
                " order by count(*) desc").fetchall()
            sources = conn.execute(
                "select source, count(*) from events where type = 'signup'"
                " group by source order by count(*) desc limit 10").fetchall()
            return {
                'visitors': row[0], 'signups': row[1],
                'conversions_done': row[2],
                'credits_consumed': float(row[3]),
                'payments_activated': row[4],
                'revenue_fcfa': int(row[5]),
                'active_users': row[6], 'paying_keys': row[7],
                'packs': [{'pack': p[0], 'count': p[1],
                           'revenue_fcfa': int(p[2]),
                           'credits_sold': int(p[3])} for p in packs],
                'signup_sources': [{'source': s[0], 'count': s[1]}
                                   for s in sources]}
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


    def connect_direct(self):
        """Connexion directe (hors pool) pour le verrou consultatif du
        cron : pré-validée comme le pool — un DSN invalide lève
        PgStoreError SANS jamais exposer la chaîne (mot de passe)."""
        params = self._validate_dsn()
        return psycopg.connect(self._dsn,
                               connect_timeout=int(self._pool_timeout),
                               **self._pool_kwargs(params))


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
    défini, sinon (None, 'json') — l'appelant garde ses magasins JSON.
    Le DSN est SANITISÉ dans PgStore (caractères invisibles de
    copier-coller : marques bidi U+200E/U+200F, BOM, zero-width…)."""
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
