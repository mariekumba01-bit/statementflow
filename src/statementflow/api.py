"""API REST StatementFlow (FastAPI).

Endpoints :
- GET  /health                     : état du service (public, sans donnée sensible)
- POST /v1/analyze                 : soumet un document (multipart) → job_id
- GET  /v1/results/{job_id}        : résultat du job (clé propriétaire requise)
- GET  /v1/usage                   : compteurs d'usage de la clé fournie
- GET  /v1/admin/usage             : usage global (ADMIN uniquement)
- POST /v1/admin/usage/reset       : réinitialisation d'un compteur (ADMIN)

Sécurité (v0.2.0 — voir security.py et docs/SECURITY.md) :
- TOUTE route privée exige X-API-Key : 401 si absente, 401 si inconnue
  (quand des hashes de clés sont configurés via STATEMENTFLOW_API_KEY_HASHES) ;
- les résultats sont liés à la clé qui a soumis le document : toute autre
  clé reçoit 404, indistinguable d'un job inconnu (pas de sonde d'existence) ;
- les clés ne sont stockées que sous forme de hash SHA-256, comparées en
  temps constant (hmac.compare_digest), jamais journalisées ni renvoyées
  (réponses masquées par mask_key) ;
- l'administration est séparée : STATEMENTFLOW_ADMIN_KEY (hashée au
  chargement) requis pour /v1/admin/*, 403 sinon ;
- quotas par clé (STATEMENTFLOW_QUOTA_DOCUMENTS / _PAGES) → 429 ;
- identifiant de requête X-Request-ID sur chaque réponse et dans chaque
  erreur structurée {'error': {'code', 'message', 'request_id'}} ;
- aucune donnée bancaire journalisée ; noms de fichiers nettoyés
  (sanitize_filename : path traversal et caractères de contrôle retirés) ;
- documents traités en mémoire, jamais écrits ; résultats TTL puis purge ;
- CORS : désactivé par défaut (même origine) — ne pas utiliser CORS comme
  mécanisme d'authentification.

Mode ouvert (développement local) : sans STATEMENTFLOW_API_KEY_HASHES,
toute clé NON VIDE est acceptée (séparation par clé et quotas actifs).
Signalé dans /health via auth_mode. Pour la production, configurez les
hashes : `statementflow keys create`.

Lancer : uvicorn statementflow.api:app --host 127.0.0.1 --port 8080
Documentation OpenAPI : http://127.0.0.1:8080/docs
"""
from __future__ import annotations

import time
import uuid

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import __version__
from .config import Config
from .engine import ParseError, parse_bytes
from .security import (AdminAuthorizer, ApiKeyStore, hash_key, mask_key,
                       sanitize_filename)
from .usage import Quota, UsageStore

config = Config.from_env()
usage_store = UsageStore(config.usage_store_path)
api_keys = ApiKeyStore.from_env()
admin_auth = AdminAuthorizer.from_env()
quota = Quota(max_documents=config.quota_documents,
              max_pages=config.quota_pages)

app = FastAPI(
    title='StatementFlow API',
    version=__version__,
    description=(
        'Conversion de relevés bancaires et exports mobile money (PDF natifs '
        'ou scannés, CSV, XLSX) en transactions JSON normalisées avec contrôles '
        'de cohérence comptable. Traitement éphémère : aucun document stocké. '
        'Authentification par clé d\'API (X-API-Key) requise sur toutes les '
        'routes privées ; administration séparée (/v1/admin/*).'
    ),
)

# codes d'erreur stables (documentés dans docs/API.md)
ERROR_CODES = {400: 'invalid_file', 401: 'unauthorized', 403: 'forbidden',
               404: 'not_found', 405: 'method_not_allowed',
               413: 'file_too_large', 422: 'processing_error',
               429: 'quota_exceeded', 500: 'internal_error'}

# stock des tâches en mémoire : {job_id: {'status', 'result'|'error',
# 'created', 'key_hash'}} — le résultat n'est lisible que par la clé
# qui a soumis le document.
_jobs: dict[str, dict] = {}


class HealthResponse(BaseModel):
    status: str
    version: str
    auth_mode: str
    limits: dict


class AnalyzeAccepted(BaseModel):
    job_id: str
    status: str
    poll: str


class AdminResetRequest(BaseModel):
    key_hash: str


# ── infrastructure : request id + erreurs structurées ───────────────
@app.middleware('http')
async def request_id_middleware(request: Request, call_next):
    rid = request.headers.get('X-Request-ID') or uuid.uuid4().hex
    request.state.request_id = rid
    response = await call_next(request)
    response.headers['X-Request-ID'] = rid
    return response


@app.exception_handler(HTTPException)
async def structured_http_error(request: Request, exc: HTTPException):
    rid = getattr(request.state, 'request_id', '')
    return JSONResponse(
        status_code=exc.status_code,
        headers=exc.headers,
        content={'error': {
            'code': ERROR_CODES.get(exc.status_code, 'http_error'),
            'message': exc.detail,
            'request_id': rid,
        }})


def _require_key(x_api_key: str | None) -> str:
    """Authentification des routes privées → clé vérifiée.
    401 si l'en-tête est absent ou vide ; 401 si la clé est inconnue
    (mode fermé). La clé en clair ne sort jamais de cette fonction."""
    if not x_api_key or not x_api_key.strip():
        raise HTTPException(
            401, 'Clé d\'API manquante : fournissez l\'en-tête X-API-Key.')
    key = x_api_key.strip()
    if not api_keys.verify(key):
        raise HTTPException(401, 'Clé d\'API invalide.')
    return key


def _require_admin(x_admin_key: str | None) -> None:
    """Autorisation administrative distincte → 403 si absente/invalide
    ou si aucune clé admin n'est configurée (routes désactivées)."""
    if not admin_auth.configured or not admin_auth.verify(x_admin_key):
        raise HTTPException(
            403, 'Opération d\'administration : clé d\'administration '
                 'requise (en-tête X-Admin-Key).')


def _sweep_jobs():
    """Supprime les résultats expirés (TTL)."""
    now = time.time()
    expired = [jid for jid, j in _jobs.items()
               if now - j['created'] > config.results_ttl_s]
    for jid in expired:
        del _jobs[jid]


# ── routes publiques (aucune donnée sensible) ───────────────────────
@app.get('/health', response_model=HealthResponse)
def health():
    return HealthResponse(
        status='ok', version=__version__, auth_mode=api_keys.mode,
        limits={'max_file_mb': config.max_file_mb, 'max_pages': config.max_pages,
                'max_rows': config.max_rows, 'results_ttl_s': config.results_ttl_s})


# ── routes privées (X-API-Key requis) ───────────────────────────────
@app.post('/v1/analyze', response_model=AnalyzeAccepted, status_code=202)
async def analyze(
    file: UploadFile = File(..., description='PDF, CSV, TSV ou XLSX'),
    x_api_key: str | None = Header(
        default=None, description='Clé d\'API (requise)'),
    x_admin_key: str | None = Header(
        default=None, alias='X-Admin-Key', include_in_schema=False),
    x_currency_hint: str | None = Header(default=None, alias='X-Currency-Hint'),
):
    _sweep_jobs()
    key = _require_key(x_api_key)
    key_hash = hash_key(key)
    data = await file.read()
    if len(data) > config.max_file_mb * 1024 * 1024:
        raise HTTPException(
            413, f'Fichier trop volumineux (limite {config.max_file_mb} Mo)')
    if not data:
        raise HTTPException(400, 'Fichier vide')
    if quota is not None and (quota.max_documents is not None
                              or quota.max_pages is not None):
        ok, reason = quota.check(usage_store.get(key_hash))
        if not ok:
            raise HTTPException(429, reason)
    filename = sanitize_filename(file.filename)
    job_id = uuid.uuid4().hex  # non dévinable
    _jobs[job_id] = {'status': 'processing', 'created': time.time(),
                     'key_hash': key_hash}
    # traitement immédiat (CPU-bound, rapide) mais réponse asynchrone :
    # le client récupère le résultat via /v1/results/{job_id}
    try:
        result = parse_bytes(data, filename, config, x_currency_hint)
        usage_store.record(key_hash, result['usage']['pages'],
                           result['usage']['transactions'])
        _jobs[job_id].update(status='done', result=result)
    except ParseError as e:
        _jobs[job_id].update(status='error', error=str(e))
    return AnalyzeAccepted(job_id=job_id, status=_jobs[job_id]['status'],
                           poll=f'/v1/results/{job_id}')


@app.get('/v1/results/{job_id}')
def results(job_id: str,
            x_api_key: str | None = Header(default=None)):
    _sweep_jobs()
    key = _require_key(x_api_key)
    job = _jobs.get(job_id)
    # 404 identique pour job inconnu et job d'une autre clé : pas de sonde
    if job is None or job['key_hash'] != hash_key(key):
        raise HTTPException(
            404, 'Résultat inconnu, expiré (TTL '
                 f'{config.results_ttl_s} s) ou non accessible avec cette '
                 'clé. Relancez l\'analyse.')
    if job['status'] == 'processing':
        return JSONResponse({'job_id': job_id, 'status': 'processing'},
                            status_code=202)
    if job['status'] == 'error':
        return JSONResponse(
            {'job_id': job_id, 'status': 'error', 'error': job['error']},
            status_code=422)
    return {'job_id': job_id, 'status': 'done', **job['result']}


@app.get('/v1/usage')
def usage(x_api_key: str | None = Header(default=None)):
    """Usage de la clé appelante (jamais celui d'une autre clé)."""
    key = _require_key(x_api_key)
    key_hash = hash_key(key)
    return {'key_masked': mask_key(key), 'key_hash': key_hash,
            **usage_store.get(key_hash)}


# ── routes d'administration (X-Admin-Key requis, distinct des clés) ─
@app.get('/v1/admin/usage')
def admin_usage(x_admin_key: str | None = Header(default=None)):
    """Usage global. Les clés n'apparaissent que sous forme de hash."""
    _require_admin(x_admin_key)
    with usage_store._lock:
        return {'records': [
            {'key_hash': k, 'documents': r.documents, 'pages': r.pages,
             'transactions': r.transactions}
            for k, r in usage_store._records.items()]}


@app.post('/v1/admin/usage/reset')
def admin_usage_reset(body: AdminResetRequest,
                      x_admin_key: str | None = Header(default=None)):
    """Réinitialise le compteur d'un hash de clé (action administrative)."""
    _require_admin(x_admin_key)
    target = body.key_hash.strip().lower()
    usage_store.reset(target)
    return {'key_hash': target, 'reset': True}
