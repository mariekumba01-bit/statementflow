"""API REST StatementFlow (FastAPI).

Endpoints :
- GET  /health                : état du service
- POST /v1/analyze            : soumet un document (multipart) → job_id (asynchrone)
- GET  /v1/results/{job_id}   : récupère le résultat JSON
- GET  /v1/usage              : compteurs d'usage de la clé fournie

Sécurité par design :
- aucun stockage des documents (traitement en mémoire, octets jetés) ;
- résultats conservés en mémoire TTL court (défaut 15 min) puis supprimés ;
- aucune donnée bancaire journalisée (seuls filename, taille, code d'erreur) ;
- limites strictes de taille et de pages (Config) ;
- clé d'API optionnelle (X-API-Key) pour les compteurs d'usage — aucune
  authentification forte par défaut : service local.

Lancer : uvicorn statementflow.api:app --host 127.0.0.1 --port 8080
Documentation OpenAPI : http://127.0.0.1:8080/docs
"""
from __future__ import annotations

import uuid

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config import Config
from .engine import ParseError, parse_bytes
from .usage import Quota, UsageStore

config = Config.from_env()
usage_store = UsageStore(config.usage_store_path)
# quota par clé : extensible, désactivé par défaut en local
quotas: dict[str, Quota] = {}

app = FastAPI(
    title='StatementFlow API',
    version='0.1.0',
    description=(
        'Conversion de relevés bancaires et exports mobile money (PDF natifs '
        'ou scannés, CSV, XLSX) en transactions JSON normalisées avec contrôles '
        'de cohérence comptable. Traitement éphémère : aucun document stocké.'
    ),
)

# stock des tâches en mémoire : {job_id: {'status', 'result'|'error', 'created'}}
_jobs: dict[str, dict] = {}


class HealthResponse(BaseModel):
    status: str
    version: str
    limits: dict


class AnalyzeAccepted(BaseModel):
    job_id: str
    status: str
    poll: str


def _sweep_jobs():
    """Supprime les résultats expirés (TTL)."""
    import time
    now = time.time()
    expired = [jid for jid, j in _jobs.items()
               if now - j['created'] > config.results_ttl_s]
    for jid in expired:
        del _jobs[jid]


@app.get('/health', response_model=HealthResponse)
def health():
    return HealthResponse(
        status='ok', version='0.1.0',
        limits={'max_file_mb': config.max_file_mb, 'max_pages': config.max_pages,
                'results_ttl_s': config.results_ttl_s})


@app.post('/v1/analyze', response_model=AnalyzeAccepted, status_code=202)
async def analyze(
    file: UploadFile = File(..., description='PDF, CSV, TSV ou XLSX'),
    x_api_key: str | None = Header(
        default=None, description='Clé optionnelle pour les compteurs d’usage'),
    x_currency_hint: str | None = Header(default=None, alias='X-Currency-Hint'),
):
    _sweep_jobs()
    data = await file.read()
    if len(data) > config.max_file_mb * 1024 * 1024:
        raise HTTPException(
            413, f'Fichier trop volumineux (limite {config.max_file_mb} Mo)')
    if not data:
        raise HTTPException(400, 'Fichier vide')
    filename = file.filename or 'document'
    key = x_api_key or 'anonymous'
    quota = quotas.get(key)
    if quota is not None:
        ok, reason = quota.check(usage_store.get(key))
        if not ok:
            raise HTTPException(429, reason)
    job_id = uuid.uuid4().hex
    _jobs[job_id] = {'status': 'processing', 'created': __import__('time').time()}
    # traitement immédiat (CPU-bound, rapide) mais réponse asynchrone :
    # le client récupère le résultat via /v1/results/{job_id}
    try:
        result = parse_bytes(data, filename, config, x_currency_hint)
        usage_store.record(key, result['usage']['pages'],
                           result['usage']['transactions'])
        _jobs[job_id].update(status='done', result=result)
    except ParseError as e:
        _jobs[job_id].update(status='error', error=str(e))
    return AnalyzeAccepted(job_id=job_id, status=_jobs[job_id]['status'],
                           poll=f'/v1/results/{job_id}')


@app.get('/v1/results/{job_id}')
def results(job_id: str):
    _sweep_jobs()
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(
            404, 'Résultat inconnu ou expiré (TTL '
                 f'{config.results_ttl_s} s). Relancez l’analyse.')
    if job['status'] == 'processing':
        return JSONResponse({'job_id': job_id, 'status': 'processing'}, status_code=202)
    if job['status'] == 'error':
        return JSONResponse(
            {'job_id': job_id, 'status': 'error', 'error': job['error']},
            status_code=422)
    return {'job_id': job_id, 'status': 'done', **job['result']}


@app.get('/v1/usage')
def usage(x_api_key: str | None = Header(default=None)):
    key = x_api_key or 'anonymous'
    return {'key': key, **usage_store.get(key)}


@app.delete('/v1/usage/{key}')
def usage_reset(key: str):
    usage_store.reset(key)
    return {'key': key, 'reset': True}
