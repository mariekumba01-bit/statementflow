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

import json
import os
import time
import uuid

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import __version__
from .billing import (CREDIT_PACKS, BillingStore, FakeSasPay, SasPayError,
                      is_failed, is_paid, provider_from_env,
                      verify_webhook_signature)
from .config import Config
from .engine import ParseError, parse_bytes
from .security import (AdminAuthorizer, ApiKeyStore, ManagedKeyStore,
                       hash_key, mask_key, sanitize_filename)
from .usage import Quota, UsageStore

config = Config.from_env()
usage_store = UsageStore(config.usage_store_path)
api_keys = ApiKeyStore.from_env()
admin_auth = AdminAuthorizer.from_env()
managed_keys = ManagedKeyStore(config.keys_path)
billing_store = BillingStore(config.billing_store_path)
# passerelle d'encaissement : SasPay (si clé secrète configurée) ou
# simulateur local (mode test, aucune connexion, aucun paiement réel)
saspay, billing_mode = provider_from_env()
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
               402: 'payment_required', 413: 'file_too_large',
               422: 'processing_error', 429: 'quota_exceeded',
               500: 'internal_error'}

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
    # clé GÉRÉE révoquée → refusée même en mode ouvert (KEYS_PATH)
    kh = hash_key(key)
    managed_keys._load()
    if managed_keys._path:
        record = managed_keys._records.get(kh)
        if record is not None and record.revoked:
            raise HTTPException(
                401, 'Clé d\'API révoquée.')
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
    # clés gérées : pré-autorisation par crédits prépayés (une clé non
    # gérée — déclarée via API_KEY_HASHES — reste non facturée)
    managed = managed_keys.lookup(key_hash)
    if managed is not None and managed.credits < 1:
        raise HTTPException(
            402, 'Crédits épuisés : rechargez sur /v1/billing/topup '
                 '(voir /v1/billing/packs).')
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
        if managed is not None:
            # décompte APRÈS un traitement réussi (règle publique :
            # 1 crédit = 1 page PDF ou 50 lignes tabulaires)
            from .billing import credits_for
            cost = credits_for(result['usage'], result['document']['kind'])
            remaining = managed_keys.deduct(key_hash, cost)
            result['billing'] = {
                'credits_charged': cost,
                'credits_remaining': round(remaining, 2) if remaining >= 0 else None,
                'billing_mode': 'managed',
            }
        else:
            result['billing'] = {'billing_mode': 'unmanaged'}
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
    """Usage et solde de crédits de la clé appelante."""
    key = _require_key(x_api_key)
    key_hash = hash_key(key)
    balance = managed_keys.balance(key_hash)
    return {'key_masked': mask_key(key), 'key_hash': key_hash,
            'credits_remaining': round(balance, 2) if balance is not None else None,
            'billing_mode': 'managed' if balance is not None else 'unmanaged',
            **usage_store.get(key_hash)}


# ── facturation prépayée ────────────────────────────────────────────
@app.get('/v1/billing/packs')
def billing_packs():
    """Catalogue public des packs de crédits (aucune donnée sensible)."""
    return {'currency': 'XOF',
            'credit_rule': '1 crédit = 1 page PDF ou 50 lignes CSV/TSV/XLSX',
            'packs': [{'pack': p, **v} for p, v in CREDIT_PACKS.items()]}


@app.post('/v1/billing/topup')
def billing_topup(body: dict,
                  x_api_key: str | None = Header(default=None)):
    """Démarre l'achat d'un pack : crée une session de paiement chez le
    prestataire (SasPay en production, simulateur en mode test) et
    renvoie l'URL de paiement à ouvrir. Les crédits sont activés
    uniquement après confirmation serveur (webhook signé + statut)."""
    key = _require_key(x_api_key)
    pack = str(body.get('pack') or '').strip().lower()
    if pack not in CREDIT_PACKS:
        raise HTTPException(
            400, f'Pack inconnu « {pack} » : voir /v1/billing/packs.')
    email = str(body.get('customer_email') or '').strip()
    name = str(body.get('customer_name') or '').strip()[:100]
    if '@' not in email or not name:
        raise HTTPException(
            400, 'customer_email (email valide) et customer_name requis.')
    label = CREDIT_PACKS[pack]['label']
    try:
        session = saspay.create_checkout(
            amount_fcfa=CREDIT_PACKS[pack]['amount_fcfa'],
            description=f'StatementFlow {label}',
            customer_email=email, customer_name=name,
            metadata={'product': 'statementflow', 'pack': pack})
    except SasPayError as e:
        raise HTTPException(502, f'Passerelle de paiement indisponible : '
                                 f'{e}') from e
    record = billing_store.create_topup(hash_key(key), pack, session)
    return {
        'reference': record['reference'],
        'pack': pack,
        'credits': record['credits'],
        'amount_fcfa': record['amount_fcfa'],
        'status': record['status'],
        'checkout_url': session.get('checkout_url', ''),
        'note': 'Crédits activés après confirmation du paiement '
                '(webhook signé + vérification serveur).',
    }


@app.get('/v1/billing/status/{reference}')
def billing_status(reference: str,
                   x_api_key: str | None = Header(default=None)):
    """Statut d'un rechargement : interroge l'état RÉEL chez le prestataire
    (jamais un statut mémorisé) et active les crédits si le paiement est
    confirmé. Le statut d'origine (pending/activated) filtre par clé."""
    key = _require_key(x_api_key)
    record = billing_store.get(reference)
    # 404 identique pour référence inconnue et référence d'une autre clé
    if record is None or record['key_hash'] != hash_key(key):
        raise HTTPException(404, 'Rechargement inconnu.')
    if record['status'] == 'pending':
        try:
            status = saspay.checkout_status(record['session_id'])
        except SasPayError as e:
            raise HTTPException(502, f'Passerelle injoignable : {e}') from e
        if is_paid(status):
            _activate_topup(record, status.get('transaction_id') or '')
            record = billing_store.get(reference) or record
        elif is_failed(status):
            billing_store.mark_failed(reference)
            record = billing_store.get(reference) or record
    return {'reference': reference, 'status': record['status'],
            'credits': record['credits'],
            'transaction_id': record['transaction_id']}


def _activate_topup(record: dict, transaction_id: str) -> Optional[dict]:
    """Active les crédits d'un rechargement payé (idempotent) : ajoute les
    crédits puis marque activé. Un échec entre les deux laisse le statut
    pending → le prochain polling/webhook retente proprement."""
    activated = billing_store.activate(record['reference'], transaction_id)
    if activated is None:
        return None  # déjà activé, remboursé ou inconnu
    managed_keys.add_credits(record['key_hash'], record['credits'])
    return activated


# ── webhook prestataire (SasPay) : signature HMAC obligatoire ────────
@app.post('/v1/billing/webhook')
async def billing_webhook(request: Request):
    """Webhook SasPay : événements transaction.*.

    Sécurité : signature HMAC-SHA256 (X-Webhook-Signature sur
    « {timestamp}.{corps brut} ») + fraîcheur ≤ 300 s, vérifiées en temps
    constant sur le corps BRUT (jamais re-sérialisé). Idempotence par
    « event:transaction_id » : un événement rejoué ne recrédite jamais.
    Un paiement n'est JAMAIS cru sur la foi de ce seul webhook : le statut
    est re-vérifié côté serveur prestataire avant activation."""
    raw = await request.body()
    signature = request.headers.get('X-Webhook-Signature', '')
    timestamp = request.headers.get('X-Webhook-Timestamp', '')
    event = request.headers.get('X-Webhook-Event', '')
    secret = os.environ.get('STATEMENTFLOW_SASPAY_WEBHOOK_SECRET', '')
    if not secret:
        # aucun secret configuré : webhook désactivé (mode test local)
        return JSONResponse({'error': {'code': 'webhook_disabled',
                                       'message': 'Webhook non configuré.',
                                       'request_id': ''}}, status_code=503)
    if not verify_webhook_signature(raw, timestamp, signature, secret):
        return JSONResponse({'error': {'code': 'invalid_signature',
                                       'message': 'Signature invalide.',
                                       'request_id': ''}}, status_code=401)
    try:
        payload = json.loads(raw)
    except ValueError:
        return JSONResponse({'error': {'code': 'invalid_payload',
                                       'message': 'Corps non JSON.',
                                       'request_id': ''}}, status_code=400)
    event = payload.get('event') or event
    data = payload.get('data') or {}
    tx_id = str(data.get('id') or '')
    event_key = f'{event}:{tx_id}'
    if billing_store.webhook_seen(event_key):
        return {'received': True, 'duplicate': True}  # idempotence
    billing_store.mark_webhook(event_key)
    if event not in ('transaction.success', 'transaction.failed'):
        return {'received': True, 'ignored': event}
    # retrouver le rechargement par transaction ou par session
    record = _find_topup_by_transaction(tx_id)
    if record is None:
        # ni par session : le webhook peut précéder notre polling ; on
        # tente par référence de session si fournie
        return {'received': True, 'matched': False}
    if event == 'transaction.failed':
        billing_store.mark_failed(record['reference'], tx_id)
        return {'received': True, 'failed': record['reference']}
    # succès : re-vérification SERVEUR obligatoire avant créditation
    try:
        tx = saspay.verify_payment(tx_id)
    except SasPayError as e:
        # injoignable : ne rien créditer, le polling rattrapera
        return JSONResponse({'error': {'code': 'gateway_unreachable',
                                       'message': f'Vérification impossible: {e}',
                                       'request_id': ''}}, status_code=502)
    if not is_paid(tx):
        billing_store.mark_failed(record['reference'], tx_id)
        return {'received': True, 'not_paid': tx.get('status')}
    activated = _activate_topup(record, tx_id)
    return {'received': True, 'activated': bool(activated)}


def _find_topup_by_transaction(tx_id: str) -> Optional[dict]:
    """Cherche le rechargement correspondant à une transaction : d'abord
    par transaction_id (post-activation), sinon par session (statut réel
    interrogé chez le prestataire, une requête par candidat)."""
    for t in billing_store.pending():
        if t['transaction_id'] == tx_id:
            return t
    for t in billing_store.pending():
        try:
            st = saspay.checkout_status(t['session_id'])
        except SasPayError:
            continue
        if st.get('transaction_id') == tx_id or t['session_id'] == tx_id:
            return t
    return None


# ── route de test du simulateur (JAMAIS active avec une vraie clé) ──
@app.post('/v1/billing/_test/pay')
async def billing_test_pay(body: dict):
    """Simulateur de paiement — SÉCURISÉ par deux conditions :
    disponible uniquement si (1) aucune clé SasPay réelle n'est configurée
    (provider = FakeSasPay local) ET (2) STATEMENTFLOW_TEST_PAYMENT=1.
    Avec une clé SasPay réelle, cette route répond 404 quoi qu'il arrive :
    aucun moyen de simuler un paiement en production. Le secret webhook
    reste requis pour activer les crédits (le matériel signé est retourné
    pour POSTer le webhook réel)."""
    if billing_mode != 'test' or not isinstance(saspay, FakeSasPay) \
            or os.environ.get('STATEMENTFLOW_TEST_PAYMENT', '') != '1':
        raise HTTPException(404, 'Ressource inconnue.')
    from .billing import FakeSasPay as _F
    assert isinstance(saspay, _F)
    session_id = str(body.get('session_id') or '')
    success = bool(body.get('success', True))
    webhook = saspay.pay_session(session_id, success=success)
    if webhook is None:
        raise HTTPException(404, 'Session de test inconnue ou déjà payée.')
    raw, ts, sig, event = webhook
    import base64
    return {'event': event,
            # corps EXACT signé, base64 pour un transport byte-safe :
            # la vérification HMAC porte sur les octets, pas sur une
            # réinterprétation JSON (échappements → différences d'octets)
            'webhook_body_b64': base64.b64encode(raw).decode(),
            'webhook_signature': sig,
            'webhook_timestamp': ts}


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
