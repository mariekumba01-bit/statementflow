"""Serveur MCP distant StatementFlow — transport Streamable HTTP (stateless).

Expose le SERVICE HÉBERGÉ (comptes, crédits, moteur de conversion,
rechargements) aux agents IA via le Model Context Protocol, sur
POST /mcp. Contrairement au serveur stdio local (mcp_server.py, moteur
sur la machine de l'utilisateur), ce serveur traite les documents du
service avec la facturation du compte appelant.

Transport (spec MCP « Streamable HTTP », versions 2024-11-05 →
2025-11-25) :
- POST /mcp : un message JSON-RPC 2.0 par requête ; réponse
  application/json. Le serveur est SANS ÉTAT : aucun Mcp-Session-Id
  n'est émis, chaque requête est autonome (initialize n'est pas exigé
  avant tools/list — compatibilité maximale des clients distants) ;
- notifications (JSON-RPC sans « id ») → 202 Accepted, corps vide ;
- GET /mcp (flux serveur initié) et DELETE /mcp (fin de session) ne
  sont pas proposés → 405 Method Not Allowed, conforme à la spécification
  pour un serveur stateless sans notifications serveur ;
- en-tête MCP-Protocol-Version : validé s'il est fourni (400 si la
  version n'est pas supportée) ;
- batches JSON-RPC (tableaux) refusés (-32600), comme l'exige MCP.

Méthodes : initialize, ping, tools/list, tools/call. Le serveur ne
déclare que la capacité « tools » (ni resources, ni prompts).

AUTHENTIFICATION — limites assumées de la version 1 :
- jeton porteur = la clé API StatementFlow du compte (schéma sf_…),
  transmise via « Authorization: Bearer » ou « X-API-Key », et validée
  par le MÊME chemin que l'API REST (_require_key : hash SHA-256,
  comparaison en temps constant, clés révoquées refusées, mode fermé
  503 si aucune clé n'est configurée) ;
- ce n'est PAS OAuth 2.1 : pas d'émission/d'expiration/rotation de
  jetons par le serveur, pas de scope délégué, pas de consentement
  par un fournisseur d'identité. La clé API est un secret durable qui
  IDENTIFIE le compte entier — quiconque la possède agit au nom du
  compte. Les rotations sont possibles (outil rotate_api_key) mais
  manuelles. Les clients exigant OAuth (certains connecteurs
  d'entreprise) ne pourront pas s'y connecter en l'état ; la feuille
  de route prévoit un fournisseur OAuth 2.1 devant ce même endpoint ;
- réponse 401 + « WWW-Authenticate: Bearer » sur toute requête sans
  clé valide : c'est le signal standard compris par les clients MCP.

SÉCURITÉ :
- chaque outil agit UNIQUEMENT pour le compte de la clé appelante :
  résultats et conversions filtrés par key_hash (un job d'un autre
  compte est une erreur « inconnu », indiscernable d'un job absent) ;
- aucun outil d'administration n'est exposé ; les outils MCP ne
  peuvent pas atteindre /v1/admin/* (ils n'ont jamais la clé admin) ;
- la clé API en clair n'est JAMAIS retournée, sauf une fois à la
  rotation demandée explicitement avec confirmation (rotate_api_key) ;
- AUCUN contournement de facturation possible : convert_document
  suit le MÊME chemin financier que POST /v1/analyze (réserve
  atomique d'un crédit → settle au coût réel → refund sur échec,
  idempotent par job_id dans PostgreSQL) ;
- aucun achat n'est exécuté par un outil : buy_credits crée la
  session de paiement et retourne l'URL ; le paiement est un geste
  humain (mobile money), l'activation reste serveur (webhook signé +
  re-vérification) ;
- anti-abus : plafond de payload avant décodage base64, Content-Type
  et versions de protocole vérifiés, noms de fichiers nettoyés
  (sanitize_filename), schémas d'entrée stricts (types, longueurs,
  motifs), listes blanches (packs, extensions), identifiants de job
  TOUJOURS dérivés côté serveur (jamais trustés du client) ;
- rate limiting mémoire par clé (STATEMENTFLOW_MCP_RATE_LIMIT_PER_MIN,
  défaut 120/min) et par IP pour les requêtes non authentifiées ;
- aucune requête SQL n'est construite ici : tout passe par les magasins
  existants paramétrés (PgStore/ManagedKeyStore) ; les handlers ne
  touchent jamais aux secrets (Supabase, SasPay, variables Render).
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import inspect
import json
import os
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import __version__
from .ratelimit import RateLimiter
from .security import hash_key, mask_key, sanitize_filename

router = APIRouter()

MCP_ENDPOINT = '/mcp'

# Versions du protocole acceptées (négociation initialize + en-tête
# MCP-Protocol-Version). 2025-11-25 est la dernière révision publiée ;
# le transport Streamable HTTP est identique sur toutes ces versions.
SUPPORTED_PROTOCOL_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26',
                               '2024-11-05')
DEFAULT_PROTOCOL_VERSION = '2025-06-18'

_JSONRPC_PARSE_ERROR = -32700
_JSONRPC_INVALID_REQUEST = -32600
_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_INVALID_PARAMS = -32602
_JSONRPC_INTERNAL_ERROR = -32603


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get('STATEMENTFLOW_' + name, '')
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


# anti-abus : par clé authentifiée (outils) et par IP (avant auth)
_mcp_limiter = RateLimiter(_env_int('MCP_RATE_LIMIT_PER_MIN', 120), 60)
_anon_limiter = RateLimiter(30, 60)
# rotation : opération sensible, 3/heure par clé (comme le tableau de bord)
_rotate_limiter = RateLimiter(3, 3600)

# Extensions acceptées par convert_document — seuls des formats que le
# moteur traite RÉELLEMENT (v0.6.1) : .xls binaire (OLE2/BIFF) n'est PAS
# supporté par openpyxl (XLSX = zip uniquement) et produisait une erreur
# de traitement APRÈS réservation de crédit ; il est refusé en amont.
# .txt reste accepté par le moteur (détecté/traité comme CSV).
_ALLOWED_EXTENSIONS = ('.pdf', '.csv', '.tsv', '.xlsx')


# ── exceptions internes ──────────────────────────────────────────────
class MCPHttpError(Exception):
    """Erreur de transport (statut HTTP, pas de corps JSON-RPC)."""

    def __init__(self, status_code: int, message: str,
                 headers: Optional[dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.headers = headers or {}


class ToolError(Exception):
    """Erreur métier d'un outil → résultat MCP isError (pas une erreur
    de protocole). `payload` optionnel : données structurées jointes au
    message (ex. job_id d'une conversion échouée)."""

    def __init__(self, message: str, payload: Optional[dict] = None):
        super().__init__(message)
        self.payload = payload


class InvalidParams(Exception):
    """Arguments invalides → erreur JSON-RPC -32602."""


# ── accès au module api (import différé : api.py importe mcp_http) ──
def _api():
    from . import api as api_mod
    return api_mod


def _field(rec: Any, name: str, default: Any = None) -> Any:
    """Champ d'un enregistrement de clé gérée : ManagedKey (JSON,
    attributs) ou dict (PgStore)."""
    if rec is None:
        return default
    if isinstance(rec, dict):
        return rec.get(name, default)
    return getattr(rec, name, default)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get('x-forwarded-for', '')
    if fwd:
        return fwd.split(',')[0].strip()[:64]
    return request.client.host if request.client else 'unknown'


# ── authentification (réutilise le verrouillage de l'API REST) ───────
def _authenticate(request: Request) -> tuple[str, str]:
    """Extrait le jeton (Authorization: Bearer ou X-API-Key) et le
    valide par le chemin existant. Retourne (clé, hash) — la clé en
    clair ne quitte jamais cette fonction. Lève MCPHttpError 401/503."""
    from fastapi import HTTPException
    api_mod = _api()
    auth = request.headers.get('authorization', '')
    token = auth[7:].strip() if auth.lower().startswith('bearer ') else ''
    if not token:
        token = request.headers.get('x-api-key', '')
    if not token:
        raise MCPHttpError(
            401, 'Authentification requise : fournissez votre clé API '
                 'StatementFlow via « Authorization: Bearer <clé> ».',
            {'WWW-Authenticate': 'Bearer'})
    try:
        key = api_mod._require_key(token)
    except HTTPException as e:
        raise MCPHttpError(e.status_code, str(e.detail),
                           {'WWW-Authenticate': 'Bearer'}
                           if e.status_code == 401 else None)
    return key, hash_key(key)


async def _read_body_capped(request: Request, max_bytes: int) -> bytes:
    """Corps de requête plafonné AVANT tout chargement mémoire complet
    (Content-Length d'abord, puis lecture par morceaux)."""
    cl = request.headers.get('content-length', '')
    if cl.isdigit() and int(cl) > max_bytes:
        raise MCPHttpError(413, f'Corps trop volumineux (limite '
                                f'{max_bytes // (1024 * 1024)} Mo).')
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise MCPHttpError(413, f'Corps trop volumineux (limite '
                                    f'{max_bytes // (1024 * 1024)} Mo).')
        chunks.append(chunk)
    return b''.join(chunks)


def _max_payload_bytes() -> int:
    """Plafond du payload MCP : document max + gonflement base64 (~4/3)
    + marge pour le JSON environnant."""
    cfg = _api().config
    return int(cfg.max_file_mb * 1024 * 1024 * 1.4) + 65536


def _user_for_key(store, key_hash: str) -> tuple[Optional[str], Optional[str]]:
    """(user_id, email) du compte propriétaire d'une clé, via les tables
    existantes — aucun secret manipulé. (None, None) pour une clé
    technique sans compte."""
    def q(conn):
        return conn.execute(
            'select k.user_id, u.email from api_keys k'
            ' join users u on u.id = k.user_id'
            ' where k.key_hash = %s', (key_hash,)).fetchone()
    try:
        row = store._call(q)
    except Exception:
        return (None, None)
    if row is None or row[0] is None:
        return (None, None)
    return (str(row[0]), row[1])


def _pg() -> Optional[Any]:
    """PgStore actif ou None (mode JSON : comptes web indisponibles)."""
    return _api()._pg_store


def _jsonify(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _require_str(args: dict, name: str, *, max_len: int = 200,
                 min_len: int = 1) -> str:
    v = args.get(name)
    if not isinstance(v, str) or not (min_len <= len(v) <= max_len):
        raise InvalidParams(
            f'« {name} » : chaîne attendue '
            f'({min_len} à {max_len} caractères).')
    return v


def _job_lookup(api_mod, job_id: str, key_hash: str):
    """Mémoire d'abord, puis PostgreSQL : un redémarrage du service ne
    rend plus un job inaccessible pendant son TTL (v0.6.1). None si
    inconnu, expiré ou d'un autre compte (indiscernables)."""
    job = api_mod._jobs.get(job_id)
    if job is not None:
        return job if job['key_hash'] == key_hash else None
    store = _pg()
    if store is not None:
        try:
            rec = store.get_job_for_key(key_hash, job_id)
        except Exception:
            rec = None
        if rec is not None:
            return {'status': rec['status'], 'result': rec['result'],
                    'error': rec['error'], 'key_hash': key_hash,
                    'created': time.time()}
    return None


# ════════════════════════════════════════════════════════════════════
# OUTILS MCP (11) — lecture (8) puis actions (3)
# Chaque handler : (clé, hash_de_clé, arguments) → dict structuré.
# ════════════════════════════════════════════════════════════════════

def _tool_get_account_info(key: str, key_hash: str, args: dict) -> dict:
    """Identité du compte appelant : email, vérification, clé masquée."""
    api_mod = _api()
    rec = api_mod.managed_keys.lookup(key_hash)
    out: dict = {
        'key_masked': mask_key(key),
        'billing_mode': 'managed' if rec is not None else 'unmanaged',
        'service_version': __version__,
    }
    store = _pg()
    if store is not None:
        user_id, email = _user_for_key(store, key_hash)
        if user_id is not None:
            def q(conn):
                return conn.execute(
                    'select email_verified, verified_via from users'
                    ' where id = %s::uuid', (user_id,)).fetchone()
            try:
                row = store._call(q)
            except Exception:
                row = None
            out['account_email'] = email
            out['email_verified'] = bool(row[0]) if row else None
            out['verified_via'] = row[1] if row else None
            return out
        out['account_email'] = None
        out['note'] = ('Clé technique sans compte client (aucun email '
                       'associé).')
        return out
    out['note'] = ('Instance sans PostgreSQL : informations de clé '
                   'uniquement (mode local).')
    if rec is not None:
        out['key_label'] = _field(rec, 'label')
        out['key_created'] = _field(rec, 'created')
    return out


def _tool_get_credits(key: str, key_hash: str, args: dict) -> dict:
    """Solde de crédits et usage de la clé appelante."""
    api_mod = _api()
    balance = api_mod.managed_keys.balance(key_hash)
    usage = api_mod.usage_store.get(key_hash)
    return {
        'credits_remaining': round(balance, 2)
        if balance is not None else None,
        'billing_mode': 'managed' if balance is not None else 'unmanaged',
        'credit_rule': '1 crédit = 1 page PDF ou 50 lignes de tableau '
                       '(CSV/TSV/XLSX) ; minimum 1 crédit par conversion '
                       'réussie ; aucun débit si le traitement échoue.',
        'usage': usage,
        'key_masked': mask_key(key),
    }


def _tool_get_supported_formats(key: str, key_hash: str,
                                args: dict) -> dict:
    """Formats et sources pris en charge par le moteur."""
    cfg = _api().config
    return {
        'input_formats': [
            {'format': 'pdf', 'details': 'PDF natifs (couche texte) et '
             'scannés (repli OCR automatique à 300 dpi, tesseract)'},
            {'format': 'csv', 'details': 'séparateur , ou ; (détection), '
             'BOM UTF-8 toléré, encodages courants'},
            {'format': 'tsv', 'details': 'tabulations, dates ISO ou '
             'régionales'},
            {'format': 'xlsx', 'details': 'classeurs Excel .xlsx '
             '(openpyxl). Le format .xls binaire legacy (OLE2/BIFF) '
             'n\'est PAS supporté : convertissez en .xlsx'},
        ],
        'allowed_extensions': list(_ALLOWED_EXTENSIONS),
        'max_file_mb': cfg.max_file_mb,
        'bank_pdf_families': [
            'France : Société Générale, BNP Paribas, Crédit Agricole, '
            'LCL, Banque Postale, Caisse d\'Épargne, Crédit Mutuel, '
            'Boursorama, Fortuneo, Monabanq',
            'Europe : Deutsche Bank, ING, Santander, Barclays',
            'UEMOA : Ecobank Sénégal, BOA Sénégal (XOF/FCFA entiers)',
        ],
        'mobile_money_sources': [
            'Wave / Wave Business (FCFA, colonne Sens)',
            'Orange Money (montant signé — devise absente du contenu : '
            'passez currency_hint, ex. « XOF »)',
            'MTN MoMo (montant signé)',
            'Moov (débit/crédit séparés)',
        ],
        'output': {
            'transactions': 'date ISO AAAA-MM-JJ, description, debit, '
                            'credit, balance, currency, statut de '
                            'validation par transaction',
            'checks': 'contrôles de cohérence comptable (chaîne des '
                      'soldes, ouverture + crédits − débits = clôture)',
            'alerts': 'alertes sur données incertaines (ocr_used, '
                      'currency_not_identified, date_unparseable…)',
        },
        'currency_policy': 'La devise n\'est JAMAIS inventée : absente '
                           'du document → currency: null + alerte ; '
                           'currency_hint la fournit.',
    }


def _tool_get_limits(key: str, key_hash: str, args: dict) -> dict:
    """Limites de traitement et quotas appliqués à la clé appelante."""
    api_mod = _api()
    cfg = api_mod.config
    quota = api_mod.quota
    return {
        'max_file_mb': cfg.max_file_mb,
        'max_pages': cfg.max_pages,
        'max_rows': cfg.max_rows,
        'results_ttl_s': cfg.results_ttl_s,
        'quota_documents_per_period': getattr(quota, 'max_documents', None),
        'quota_pages_per_period': getattr(quota, 'max_pages', None),
        'mcp_rate_limit_per_minute': _mcp_limiter.max_events,
        'base64_note': 'convert_document accepte le document encodé en '
                       'base64 (ASCII) ; la limite de taille s\'applique '
                       'au document DÉCODÉ.',
        'service_version': __version__,
    }


def _tool_list_conversions(key: str, key_hash: str, args: dict) -> dict:
    """Historique des conversions du compte appelant."""
    api_mod = _api()
    limit = args.get('limit', 20)
    offset = args.get('offset', 0)
    if not isinstance(limit, int) or isinstance(limit, bool) \
            or not (1 <= limit <= 100):
        raise InvalidParams('« limit » : entier 1 à 100 attendu.')
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise InvalidParams('« offset » : entier positif attendu.')
    store = _pg()
    if store is not None:
        user_id, _ = _user_for_key(store, key_hash)
        if user_id is not None:
            records = store.list_jobs_for_user(user_id, limit=limit,
                                               offset=offset)
        else:
            # clé technique (sans compte web) : l'historique existe
            # aussi dans la table jobs, filtré par la clé appelante
            records = store.list_jobs_for_key(key_hash, limit=limit,
                                              offset=offset)
        return {'records': records}
    api_mod._sweep_jobs()
    records = [
        {'job_id': jid, 'status': job['status'],
         'created': job['created'],
         'transactions': (len(job['result'].get('transactions', []))
                          if job.get('result') else None)}
        for jid, job in sorted(api_mod._jobs.items(),
                               key=lambda kv: -kv[1]['created'])
        if job['key_hash'] == key_hash
    ][offset:offset + limit]
    return {'records': records,
            'note': 'Instance sans PostgreSQL : historique mémoire '
                    'limité au TTL des résultats.'}


def _tool_get_conversion_status(key: str, key_hash: str,
                                args: dict) -> dict:
    """Statut d'un traitement (conversion) : processing / done / error.
    Couvre « récupérer le statut d'un traitement » et « vérifier le
    statut d'une conversion » — même opération."""
    api_mod = _api()
    job_id = _require_str(args, 'job_id', max_len=64)
    if not all(c in '0123456789abcdef' for c in job_id.lower()):
        raise InvalidParams('« job_id » : identifiant hexadécimal attendu.')
    api_mod._sweep_jobs()
    job = _job_lookup(api_mod, job_id, key_hash)
    if job is None:
        raise ToolError('Conversion inconnue, expirée (TTL '
                        f'{api_mod.config.results_ttl_s} s) ou non '
                        'accessible depuis ce compte.')
    out: dict = {'job_id': job_id, 'status': job['status']}
    if job['status'] == 'done':
        result = job.get('result') or {}
        out['usage'] = result.get('usage')
        out['billing'] = result.get('billing')
        out['transactions_count'] = len(result.get('transactions', []))
        out['next_step'] = ('Appelez get_result avec ce job_id pour le '
                            'résultat complet.')
    elif job['status'] == 'error':
        out['error'] = job.get('error')
        out['credits_charged'] = 0
    return out


def _tool_get_result(key: str, key_hash: str, args: dict) -> dict:
    """Résultat complet d'une conversion réussie (transactions,
    contrôles, usage, facturation)."""
    api_mod = _api()
    job_id = _require_str(args, 'job_id', max_len=64)
    if not all(c in '0123456789abcdef' for c in job_id.lower()):
        raise InvalidParams('« job_id » : identifiant hexadécimal attendu.')
    api_mod._sweep_jobs()
    job = _job_lookup(api_mod, job_id, key_hash)
    if job is None:
        raise ToolError('Résultat inconnu, expiré (TTL '
                        f'{api_mod.config.results_ttl_s} s) ou non '
                        'accessible depuis ce compte.')
    if job['status'] == 'processing':
        return {'job_id': job_id, 'status': 'processing',
                'retry_hint': 'Interrogez à nouveau get_result ou '
                              'get_conversion_status dans quelques '
                              'secondes.'}
    if job['status'] == 'error':
        raise ToolError(f'Conversion échouée : {job.get("error")}',
                        payload={'job_id': job_id, 'status': 'error',
                                 'error': job.get('error')})
    if not job.get('result'):
        # job 'done' antérieur à la persistance des résultats (v0.6.1) :
        # le statut est connu, le contenu n'est plus récupérable
        return {'job_id': job_id, 'status': 'done',
                'note': 'Conversion terminée avant la persistance des '
                        'résultats (v0.6.1) : le contenu n\'est plus '
                        'récupérable. Relancez la conversion.'}
    return {'job_id': job_id, 'status': 'done', **job['result']}


def _tool_get_pricing(key: str, key_hash: str, args: dict) -> dict:
    """Catalogue des packs de crédits et règles de facturation."""
    from .billing import CREDIT_PACKS
    return {
        'currency': 'XOF',
        'credit_rule': '1 crédit = 1 page PDF ou 50 lignes CSV/TSV/XLSX '
                       '(minimum 1 crédit par conversion réussie) ; '
                       'aucun débit si le traitement échoue ; les crédits '
                       'n\'expirent pas.',
        'packs': [{'pack': p, **v} for p, v in CREDIT_PACKS.items()],
        'payment': 'Paiement mobile money (Wave, Orange Money, Free '
                   'Money, Wizall…) via la passerelle SasPay — initié '
                   'par l\'outil buy_credits (confirmation explicite), '
                   'finalisé par un humain sur l\'URL de paiement.',
        'hint': 'Prix effectif par page : Starter 10 FCFA, Pro 8 FCFA, '
                'Business 6 FCFA.',
    }


async def _tool_convert_document(key: str, key_hash: str,
                                 args: dict) -> dict:
    """Convertit un document (base64) via le moteur du service — MÊME
    chemin financier que l'API REST : réservation atomique d'un crédit,
    complément au coût réel, restitution intégrale sur échec."""
    from fastapi.concurrency import run_in_threadpool

    from .billing import credits_for

    api_mod = _api()
    filename = sanitize_filename(_require_str(args, 'filename',
                                              max_len=200))
    if not filename.lower().endswith(_ALLOWED_EXTENSIONS):
        raise InvalidParams(
            '« filename » : extension non supportée (attendues : '
            + ', '.join(_ALLOWED_EXTENSIONS) + ').')
    b64 = args.get('data_base64')
    if not isinstance(b64, str) or not b64:
        raise InvalidParams('« data_base64 » : chaîne base64 du document '
                            'requise.')
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise InvalidParams('« data_base64 » : encodage base64 invalide.')
    max_bytes = int(api_mod.config.max_file_mb * 1024 * 1024)
    if len(data) > max_bytes:
        raise ToolError(f'Fichier trop volumineux (limite '
                        f'{api_mod.config.max_file_mb} Mo).')
    if not data:
        raise InvalidParams('Document vide.')
    currency_hint = args.get('currency_hint') or None
    if currency_hint is not None:
        if not isinstance(currency_hint, str) or not (1 <= len(currency_hint) <= 16):
            raise InvalidParams('« currency_hint » : code devise court '
                                'attendu (ex. « XOF »), ou absent.')
    idem = args.get('idempotency_key')
    if idem is not None:
        if not isinstance(idem, str) or not (8 <= len(idem) <= 200):
            raise InvalidParams('« idempotency_key » : chaîne de 8 à 200 '
                                'caractères, ou absent.')
        # identifiant de job DÉRIVÉ côté serveur, lié à la clé : rejouer
        # la même clé d'idempotence renvoie le même job sans re-débit
        job_id = hmac.new(key_hash.encode(),
                          ('mcp:' + idem).encode(),
                          hashlib.sha256).hexdigest()[:32]
    else:
        job_id = uuid.uuid4().hex

    api_mod._sweep_jobs()
    existing = api_mod._jobs.get(job_id)
    if existing is not None and existing.get('key_hash') == key_hash:
        return {'job_id': job_id, 'status': existing['status'],
                'idempotent_replay': True,
                'note': 'Une conversion avec cette idempotency_key existe '
                        'déjà : son statut est renvoyé sans nouveau '
                        'traitement ni nouveau débit.',
                'next_step': 'get_conversion_status / get_result avec ce '
                             'job_id.'}
    # rejeu après un REDÉMARRAGE (le job n'est plus en mémoire) : la
    # réservation SQL (job:<id>, unicité credit_ledger) existe encore
    # pendant le TTL — re-réserver lèverait une violation de contrainte.
    # On relit le job depuis PostgreSQL : s'il existe et appartient à la
    # clé, on renvoie son statut (rejeu idempotent, aucun re-débit) ;
    # sinon (expiré ou inconnu) la conversion repart proprement.
    if idem is not None and _pg() is not None:
        try:
            rec = _pg().get_job_for_key(key_hash, job_id)
        except Exception:
            rec = None
        if rec is not None:
            return {'job_id': job_id, 'status': rec['status'],
                    'idempotent_replay': True,
                    'note': 'Conversion déjà enregistrée pour cette '
                            'idempotency_key (récupérée depuis la base '
                            'après redémarrage) : aucun nouveau débit.',
                    'next_step': 'get_conversion_status / get_result avec '
                                 'ce job_id.'}

    # quota (même règle que POST /v1/analyze)
    quota = api_mod.quota
    if quota is not None and (quota.max_documents is not None
                              or quota.max_pages is not None):
        ok, reason = quota.check(api_mod.usage_store.get(key_hash))
        if not ok:
            raise ToolError(f'Quota dépassé : {reason}')

    managed = api_mod.managed_keys.lookup(key_hash)
    reserved = 0.0
    if managed is not None:
        if api_mod._pg_backend(api_mod.managed_keys):
            ok = api_mod.managed_keys.reserve(key_hash, 1.0, job_id=job_id)
        else:
            ok = api_mod.managed_keys.reserve(key_hash, 1.0)
        if not ok:
            raise ToolError('Crédits épuisés : consultez get_pricing puis '
                            'achetez un pack avec buy_credits.')
        reserved = 1.0
    api_mod._jobs[job_id] = {'status': 'processing',
                             'created': time.time(),
                             'key_hash': key_hash}
    if api_mod._pg_backend(api_mod.managed_keys):
        try:
            api_mod.managed_keys.record_job(
                job_id, key_hash, filename,
                api_mod.detect_kind_hint(filename), 'processing')
        except Exception:
            pass
    try:
        result = await run_in_threadpool(
            api_mod.parse_bytes, data, filename, api_mod.config,
            currency_hint)
        api_mod.usage_store.record(key_hash,
                                   result['usage']['pages'],
                                   result['usage']['transactions'])
        if managed is not None:
            cost = credits_for(result['usage'], result['document']['kind'])
            if api_mod._pg_backend(api_mod.managed_keys):
                remaining = api_mod.managed_keys.settle(
                    key_hash, cost - reserved, job_id=job_id)
            else:
                remaining = api_mod.managed_keys.settle(
                    key_hash, cost - reserved)
            result['billing'] = {
                'credits_charged': cost,
                'credits_remaining': round(remaining, 2)
                if remaining is not None and remaining >= 0 else None,
                'billing_mode': 'managed',
            }
        else:
            result['billing'] = {'billing_mode': 'unmanaged'}
        api_mod._jobs[job_id].update(status='done', result=result)
        api_mod._record_job_done(job_id, key_hash, filename, result)
        return {
            'job_id': job_id,
            'status': 'done',
            'filename': filename,
            'transactions_count': len(result.get('transactions', [])),
            'usage': result['usage'],
            'billing': result['billing'],
            'next_step': 'Appelez get_result avec ce job_id pour le '
                         'résultat complet (transactions, contrôles).',
        }
    except Exception as e:
        if managed is not None and reserved:
            if api_mod._pg_backend(api_mod.managed_keys):
                api_mod.managed_keys.refund(key_hash, reserved,
                                            job_id=job_id)
            else:
                api_mod.managed_keys.refund(key_hash, reserved)
        api_mod._jobs[job_id].update(status='error', error=str(e)[:500])
        if api_mod._pg_backend(api_mod.managed_keys):
            try:
                api_mod.managed_keys.record_job(
                    job_id, key_hash, filename,
                    api_mod.detect_kind_hint(filename), 'error',
                    error=str(e)[:500])
            except Exception:
                pass
        raise ToolError(
            f'Conversion échouée : {str(e)[:300]}',
            payload={'job_id': job_id, 'status': 'error',
                     'error': str(e)[:500],
                     'credits_charged': 0,
                     'note': 'Échec de traitement : la réservation est '
                             'restituée, aucun crédit n\'est consommé.'})


def _tool_rotate_api_key(key: str, key_hash: str, args: dict) -> dict:
    """Régénère la clé API du compte (confirmation explicite requise).
    L'ancienne clé est révoquée immédiatement et le solde transféré."""
    from .accounts import new_token
    if args.get('confirm') is not True:
        raise ToolError(
            'Confirmation explicite requise : rappelez l\'outil avec '
            '{"confirm": true}. La rotation RÉVOQUE immédiatement la clé '
            'actuelle (celle de cette connexion MCP incluse) — assurez-'
            'vous de pouvoir mettre à jour la configuration de votre '
            'client MCP.')
    if not _rotate_limiter.allow('key:' + key_hash):
        raise ToolError('Trop de régénérations (maximum 3 par heure).')
    api_mod = _api()
    store = _pg()
    if store is not None:
        user_id, _ = _user_for_key(store, key_hash)
        if user_id is None:
            raise ToolError('Clé technique sans compte client : rotation '
                            'impossible depuis MCP.')
        new_key, new_hash = new_token()
        balance = store.rotate_key(user_id, new_hash)
    else:
        rec = api_mod.managed_keys.lookup(key_hash)
        if rec is None:
            raise ToolError('Clé non gérée : rotation impossible.')
        balance = api_mod.managed_keys.balance(key_hash) or 0.0
        # issue() génère sa propre clé : c'est ELLE qu'on retourne (le
        # hash retourné par new_token ne correspond à aucun enregistrement)
        new_key, _new_rec = api_mod.managed_keys.issue(
            _field(rec, 'label') or 'mcp-rotation', credits=balance)
        api_mod.managed_keys.revoke(key_hash)
    return {
        'status': 'rotated',
        # nouvelle clé visible UNE SEULE FOIS, dans cette réponse —
        # c'est l'objet même de l'outil (rotation demandée et confirmée)
        'api_key': new_key,
        'api_key_display': 'once',
        'credits_transferred': round(balance, 2),
        'warning': 'L\'ancienne clé est révoquée IMMÉDIATEMENT : mettez à '
                   'jour la configuration de votre client MCP avec la '
                   'nouvelle clé (Authorization: Bearer). En cas de perte '
                   'de la nouvelle clé, régénérez depuis le tableau de '
                   'bord web.',
    }


def _tool_buy_credits(key: str, key_hash: str, args: dict) -> dict:
    """Initie l'achat d'un pack de crédits (confirmation explicite
    requise) : crée la session de paiement et retourne l'URL à ouvrir.
    Aucun paiement n'est exécuté par l'outil."""
    from .billing import CREDIT_PACKS, SasPayError
    if args.get('confirm') is not True:
        raise ToolError(
            'Confirmation explicite requise : rappelez l\'outil avec '
            '{"confirm": true}. Aucun montant n\'est prélevé par cet '
            'outil : il crée une session de paiement que le client '
            'humain finalise (mobile money).')
    api_mod = _api()
    pack = str(args.get('pack') or '').strip().lower()
    if pack not in CREDIT_PACKS:
        raise ToolError(f'Pack inconnu « {pack} » : appelez get_pricing '
                        'pour le catalogue.')
    email = str(args.get('customer_email') or '').strip()[:200]
    name = str(args.get('customer_name') or '').strip()[:100]
    if not email:
        store = _pg()
        if store is not None:
            _, acct_email = _user_for_key(store, key_hash)
            email = acct_email or ''
    if not name and email and '@' in email:
        name = email.split('@')[0][:60]
    if not email or '@' not in email or '.' not in email.split('@')[-1]:
        raise ToolError('Email client requis pour la passerelle de '
                        'paiement : passez « customer_email » (ou liez '
                        'la clé à un compte web).')
    try:
        session = api_mod.saspay.create_checkout(
            amount_fcfa=CREDIT_PACKS[pack]['amount_fcfa'],
            description=f"StatementFlow {CREDIT_PACKS[pack]['label']}",
            customer_email=email,
            customer_name=name or email.split('@')[0][:60],
            metadata={'product': 'statementflow', 'pack': pack,
                      'channel': 'mcp'})
    except SasPayError as e:
        raise ToolError(f'Passerelle de paiement indisponible : {e}')
    record = api_mod.billing_store.create_topup(key_hash, pack, session)
    return {
        'reference': record['reference'],
        'pack': pack,
        'credits': record['credits'],
        'amount_fcfa': record['amount_fcfa'],
        'currency': 'XOF',
        'status': record['status'],
        'checkout_url': session.get('checkout_url', ''),
        'next_step': 'Ouvrez checkout_url dans un navigateur et payez '
                     '(Wave, Orange Money…). Les crédits sont activés '
                     'après confirmation serveur ; vérifiez ensuite avec '
                     'get_credits.',
        'note': 'Aucun prélèvement automatique : le paiement est un geste '
                'humain sur la page de la passerelle.',
    }


# ── registre des outils (nom, description, schéma, handler, lecture?) ──
_TOOLS_SPEC: list[dict] = [
    {
        'name': 'get_account_info',
        'description': "Identité du compte StatementFlow appelant : email, "
                       "vérification, clé API masquée, mode de facturation. "
                       "Ne retourne JAMAIS la clé complète.",
        'readOnly': True,
        'schema': {'type': 'object', 'properties': {},
                   'additionalProperties': False},
        'handler': _tool_get_account_info,
    },
    {
        'name': 'get_credits',
        'description': "Solde de crédits restant et usage de la clé "
                       "appelante (documents, pages traités).",
        'readOnly': True,
        'schema': {'type': 'object', 'properties': {},
                   'additionalProperties': False},
        'handler': _tool_get_credits,
    },
    {
        'name': 'get_supported_formats',
        'description': "Formats d'entrée pris en charge (PDF natifs et "
                       "scannés, CSV, TSV, XLSX — pas le .xls binaire "
                       "legacy), banques et opérateurs "
                       "mobile money reconnus (Wave, Orange Money, MTN "
                       "MoMo, Moov), et structure du JSON produit.",
        'readOnly': True,
        'schema': {'type': 'object', 'properties': {},
                   'additionalProperties': False},
        'handler': _tool_get_supported_formats,
    },
    {
        'name': 'get_limits',
        'description': "Limites de conversion appliquées au compte : "
                       "taille de fichier, pages, lignes, durée de "
                       "rétention des résultats, quotas et débit MCP.",
        'readOnly': True,
        'schema': {'type': 'object', 'properties': {},
                   'additionalProperties': False},
        'handler': _tool_get_limits,
    },
    {
        'name': 'list_conversions',
        'description': "Historique des conversions du compte appelant "
                       "(les plus récentes d'abord).",
        'readOnly': True,
        'schema': {
            'type': 'object',
            'properties': {
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100,
                          'default': 20,
                          'description': "Nombre de conversions (défaut 20)."},
                'offset': {'type': 'integer', 'minimum': 0, 'default': 0,
                           'description': "Décalage de pagination."},
            },
            'additionalProperties': False},
        'handler': _tool_list_conversions,
    },
    {
        'name': 'get_conversion_status',
        'description': "Statut d'une conversion (job_id) : processing, "
                       "done ou error, avec usage et facturation si "
                       "terminée. Sert aussi bien à récupérer le statut "
                       "d'un traitement qu'à vérifier une conversion.",
        'readOnly': True,
        'schema': {
            'type': 'object',
            'properties': {
                'job_id': {'type': 'string', 'pattern': '^[a-f0-9]{32,64}$',
                           'description': "Identifiant de conversion."},
            },
            'required': ['job_id'],
            'additionalProperties': False},
        'handler': _tool_get_conversion_status,
    },
    {
        'name': 'get_result',
        'description': "Résultat complet d'une conversion réussie : "
                       "transactions normalisées (date ISO, débit/crédit, "
                       "solde, devise), contrôles de cohérence comptable, "
                       "alertes et facturation.",
        'readOnly': True,
        'schema': {
            'type': 'object',
            'properties': {
                'job_id': {'type': 'string', 'pattern': '^[a-f0-9]{32,64}$',
                           'description': "Identifiant de conversion."},
            },
            'required': ['job_id'],
            'additionalProperties': False},
        'handler': _tool_get_result,
    },
    {
        'name': 'get_pricing',
        'description': "Catalogue des packs de crédits (montants FCFA) et "
                       "règle de facturation (1 crédit = 1 page PDF ou 50 "
                       "lignes de tableau).",
        'readOnly': True,
        'schema': {'type': 'object', 'properties': {},
                   'additionalProperties': False},
        'handler': _tool_get_pricing,
    },
    {
        'name': 'convert_document',
        'description': "Convertit un relevé bancaire ou mobile money en "
                       "JSON normalisé via le moteur du service "
                       "StatementFlow. Fournissez le nom de fichier (avec "
                       "une extension valide : .pdf .csv .tsv .xlsx — le "
                       ".xls binaire legacy n'est pas supporté) et "
                       "son contenu encodé en base64. La conversion est "
                       "facturée aux crédits du compte (1 crédit = 1 page "
                       "PDF ou 50 lignes de tableau ; aucun débit si le "
                       "traitement échoue). N'utilisez cet outil que sur "
                       "des documents que vous êtes autorisé à traiter. "
                       "Si la devise est absente du document (fréquent "
                       "avec Orange Money), passez currency_hint (ex. "
                       "\"XOF\") — elle n'est jamais inventée. Pour éviter "
                       "un double traitement en cas de rejeu, passez "
                       "idempotency_key : un rejeu renvoie le job existant "
                       "sans nouveau débit.",
        'readOnly': False,
        'schema': {
            'type': 'object',
            'properties': {
                'filename': {'type': 'string', 'minLength': 1,
                             'maxLength': 200,
                             'description': "Nom du fichier avec extension "
                                            "(.pdf, .csv, .tsv, .xlsx ; "
                                            ".xls binaire non supporté)."},
                'data_base64': {'type': 'string', 'minLength': 4,
                                'description': "Contenu binaire du document "
                                               "encodé en base64 (ASCII)."},
                'currency_hint': {'type': 'string', 'minLength': 1,
                                  'maxLength': 16,
                                  'description': "Code devise attendu si "
                                                 "absent du document (ex. "
                                                 "\"XOF\"). Optionnel."},
                'idempotency_key': {'type': 'string', 'minLength': 8,
                                    'maxLength': 200,
                                    'description': "Clé d'idempotence "
                                                   "choisie par l'appelant : "
                                                   "un rejeu renvoie le même "
                                                   "job sans re-débit. "
                                                   "Optionnel."},
            },
            'required': ['filename', 'data_base64'],
            'additionalProperties': False},
        'handler': _tool_convert_document,
    },
    {
        'name': 'rotate_api_key',
        'description': "Régénère la clé API du compte (l'ancienne est "
                       "RÉVOQUÉE immédiatement, le solde est transféré). "
                       "Destructif pour la connexion en cours : exige la "
                       "confirmation explicite confirm=true, et la "
                       "nouvelle clé n'est visible qu'une seule fois dans "
                       "la réponse. Mettez ensuite à jour votre client MCP.",
        'readOnly': False,
        'schema': {
            'type': 'object',
            'properties': {
                'confirm': {'type': 'boolean',
                            'description': "Doit valoir true pour exécuter "
                                           "la rotation."},
            },
            'required': ['confirm'],
            'additionalProperties': False},
        'handler': _tool_rotate_api_key,
    },
    {
        'name': 'buy_credits',
        'description': "Initie l'achat d'un pack de crédits (confirmation "
                       "explicite confirm=true requise) : crée une session "
                       "de paiement et retourne l'URL de paiement "
                       "(checkout_url) à ouvrir dans un navigateur. "
                       "Aucun prélèvement n'est effectué par l'outil : le "
                       "paiement (Wave, Orange Money…) est finalisé par un "
                       "humain, et les crédits sont activés après "
                       "confirmation serveur.",
        'readOnly': False,
        'schema': {
            'type': 'object',
            'properties': {
                'pack': {'type': 'string',
                         'enum': ['starter', 'pro', 'business'],
                         'description': "Pack à acheter (voir get_pricing)."},
                'confirm': {'type': 'boolean',
                            'description': "Doit valoir true pour créer la "
                                           "session de paiement."},
                'customer_email': {'type': 'string', 'maxLength': 200,
                                   'description': "Email du payeur "
                                                  "(requis si la clé n'est "
                                                  "pas liée à un compte "
                                                  "web)."},
                'customer_name': {'type': 'string', 'maxLength': 100,
                                  'description': "Nom du payeur (optionnel)."},
            },
            'required': ['pack', 'confirm'],
            'additionalProperties': False},
        'handler': _tool_buy_credits,
    },
]

TOOLS = {t['name']: t for t in _TOOLS_SPEC}


def _tools_payload() -> list[dict]:
    out = []
    for t in _TOOLS_SPEC:
        entry = {'name': t['name'], 'description': t['description'],
                 'inputSchema': t['schema']}
        hints = {'readOnlyHint': bool(t.get('readOnly'))}
        if t['name'] == 'rotate_api_key':
            hints['destructiveHint'] = True
        if t['name'] == 'convert_document':
            hints['idempotencyHint'] = True
        entry['annotations'] = hints
        out.append(entry)
    return out


# ════════════════════════════════════════════════════════════════════
# Couche JSON-RPC 2.0
# ════════════════════════════════════════════════════════════════════

def _jsonrpc_response(msg_id: Any, result: dict) -> JSONResponse:
    return JSONResponse({'jsonrpc': '2.0', 'id': msg_id, 'result': result},
                        media_type='application/json')


def _jsonrpc_error(msg_id: Any, code: int, message: str,
                   data: Optional[dict] = None) -> JSONResponse:
    err: dict = {'code': code, 'message': message}
    if data:
        err['data'] = data
    return JSONResponse({'jsonrpc': '2.0', 'id': msg_id, 'error': err},
                        media_type='application/json')


async def _dispatch(msg: dict, key: str, key_hash: str) -> Response:
    """Exécute un message JSON-RPC authentifié."""
    msg_id = msg.get('id')
    method = msg.get('method')
    params = msg.get('params')
    if not isinstance(method, str) or not method:
        return _jsonrpc_error(msg_id, _JSONRPC_INVALID_REQUEST,
                              'Champ « method » manquant ou invalide.')
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _jsonrpc_error(msg_id, _JSONRPC_INVALID_REQUEST,
                              '« params » doit être un objet.')

    if method == 'initialize':
        requested = params.get('protocolVersion')
        negotiated = (requested if requested in SUPPORTED_PROTOCOL_VERSIONS
                      else DEFAULT_PROTOCOL_VERSION)
        return _jsonrpc_response(msg_id, {
            'protocolVersion': negotiated,
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': 'statementflow',
                           'version': __version__,
                           'title': 'StatementFlow'},
            'instructions': (
                'Convertissez des relevés bancaires et mobile money '
                '(PDF natifs ou scannés, CSV, TSV, XLSX — Wave, Orange '
                'Money, MTN MoMo, Moov, banques FR/EU/UEMOA) en '
                'transactions JSON normalisées avec contrôles '
                'comptables. Commencez par get_credits et '
                'get_supported_formats ; convertissez avec '
                'convert_document (base64) puis récupérez avec '
                'get_result. La facturation est réelle : 1 crédit = 1 '
                'page PDF ou 50 lignes de tableau, aucun débit sur '
                'échec. N\'utilisez ces outils que sur des documents '
                'que vous êtes autorisé à traiter.'),
        })

    if method == 'ping':
        return _jsonrpc_response(msg_id, {})

    if method == 'tools/list':
        return _jsonrpc_response(msg_id, {'tools': _tools_payload()})

    if method == 'tools/call':
        name = params.get('name')
        arguments = params.get('arguments')
        if arguments is None:
            arguments = {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _jsonrpc_error(
                msg_id, _JSONRPC_INVALID_PARAMS,
                '« name » (chaîne) et « arguments » (objet) attendus.')
        tool = TOOLS.get(name)
        if tool is None:
            return _jsonrpc_error(msg_id, _JSONRPC_INVALID_PARAMS,
                                  f'Outil inconnu : {name}.',
                                  {'available': sorted(TOOLS)})
        try:
            res = tool['handler'](key, key_hash, arguments)
            if inspect.isawaitable(res):
                res = await res
        except InvalidParams as e:
            return _jsonrpc_error(msg_id, _JSONRPC_INVALID_PARAMS,
                                  str(e), {'tool': name})
        except ToolError as e:
            payload = e.payload if e.payload is not None \
                else {'error': str(e)}
            return _jsonrpc_response(msg_id, {
                'content': [{'type': 'text',
                             'text': _jsonify(payload)}],
                'structuredContent': payload,
                'isError': True,
            })
        except Exception as e:  # défensif : jamais de trace interne
            return _jsonrpc_response(msg_id, {
                'content': [{'type': 'text',
                             'text': _jsonify(
                                 {'error': f'Erreur interne : '
                                           f'{str(e)[:200]}'})}],
                'isError': True,
            })
        return _jsonrpc_response(msg_id, {
            'content': [{'type': 'text', 'text': _jsonify(res)}],
            'structuredContent': res,
            'isError': False,
        })

    return _jsonrpc_error(msg_id, _JSONRPC_METHOD_NOT_FOUND,
                          f'Méthode inconnue : {method}. Ce serveur '
                          'expose initialize, ping, tools/list et '
                          'tools/call (capacité « tools » uniquement).')


# ════════════════════════════════════════════════════════════════════
# Routes HTTP (transport Streamable HTTP, serveur stateless)
# ════════════════════════════════════════════════════════════════════

@router.post(MCP_ENDPOINT)
async def mcp_post(request: Request):
    try:
        # Content-Type strict : un seul format de transport
        ct = (request.headers.get('content-type') or ''
              ).split(';')[0].strip().lower()
        if ct != 'application/json':
            raise MCPHttpError(415, 'Content-Type application/json requis '
                                    'pour le transport MCP.')
        # version de protocole (validée si fournie)
        pv = request.headers.get('mcp-protocol-version', '')
        if pv and pv not in SUPPORTED_PROTOCOL_VERSIONS:
            raise MCPHttpError(
                400, f'Version de protocole MCP non supportée : {pv} '
                     f'(supportées : {", ".join(SUPPORTED_PROTOCOL_VERSIONS)}).')
        # authentification (erreurs de transport AVANT la lecture du corps)
        key, key_hash = _authenticate(request)
        # anti-abus par clé authentifiée
        if not _mcp_limiter.allow('key:' + key_hash):
            raise MCPHttpError(429, 'Trop de requêtes MCP : réessayez '
                                    'dans une minute.')
        body = await _read_body_capped(request, _max_payload_bytes())
        try:
            msg = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return _jsonrpc_error(None, _JSONRPC_PARSE_ERROR,
                                  'Corps de requête non JSON valide.')
        if not isinstance(msg, dict):
            # les batches JSON-RPC sont interdits par MCP
            return _jsonrpc_error(None, _JSONRPC_INVALID_REQUEST,
                                  'Requête JSON-RPC invalide : un objet '
                                  'unique est attendu (batch interdit).')
        if 'id' not in msg:
            # notification → 202 Accepted sans corps (spec MCP)
            return Response(status_code=202)
        return await _dispatch(msg, key, key_hash)
    except MCPHttpError as e:
        return JSONResponse(
            {'error': {'code': 'mcp_transport_error',
                       'message': e.message}},
            status_code=e.status_code, headers=e.headers)


@router.get(MCP_ENDPOINT)
async def mcp_get():
    """Flux serveur initié (SSE) non proposé : serveur stateless sans
    notifications serveur → 405, conforme à la spécification MCP."""
    return Response(status_code=405, headers={'Allow': 'POST'})


@router.delete(MCP_ENDPOINT)
async def mcp_delete():
    """Terminaison de session non applicable (stateless) → 405."""
    return Response(status_code=405, headers={'Allow': 'POST'})
