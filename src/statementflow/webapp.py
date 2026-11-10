"""Application web commerciale (v0.5.0) : comptes clients, tableau de
bord, paiements, tracking — montée sur l'API existante.

Routes (préfixe /v1, voir docs/FRONTEND.md) :
- POST /v1/auth/signup|login|logout|verify|verify/resend|
       password/forgot|password/reset ; GET /v1/auth/session
- GET  /v1/account/summary|conversions|topups ;
       POST /v1/account/convert (multipart) | /v1/account/topup {pack} |
       /v1/account/key/rotate ; GET /v1/account/result/{job_id} |
       /v1/account/topup/{reference}
- POST /v1/track (visites publiques, sans donnée personnelle)
- GET  /v1/admin/stats (X-Admin-Key)

Sécurité :
- sessions par cookie HttpOnly/SameSite=Lax (token 256 bits, stocké
  HACHÉ en base, TTL 30 jours) — le token en clair ne vit que dans le
  cookie ; pas de JavaScript y accède (HttpOnly) ;
- mots de passe PBKDF2-HMAC-SHA256 (600 000 itérations, sel unique) ;
- rate limiting mémoire sur inscription/connexion/reset/tracking ;
- isolation stricte par user_id : TOUTE lecture (résultat, historique,
  rechargement) est filtrée par le compte de la session — un job ou un
  paiement d'un autre compte est un 404 indiscernable d'un inconnu ;
- la conversion du tableau de bord passe par le MÊME chemin financier
  que l'API (reserve → settle/refund, idempotent par job_id) : les
  crédits du compte sont ceux du grand livre PostgreSQL ;
- les montants d'achat sont TOUJOURS calculés côté serveur depuis le
  catalogue CREDIT_PACKS — jamais depuis le navigateur ;
- aucun secret n'est exposé : la clé API n'est visible qu'une fois à
  la création/rotation, la session ne transite que par cookie.

Le frontend statique (web/) est servi par mount_webapp() en dernier
recours (les routes /v1 et /docs passent avant).
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import (APIRouter, File, Header, HTTPException, Request,
                     UploadFile)
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .accounts import (RESET_TOKEN_TTL_S, SESSION_TTL_S,
                       VERIFY_TOKEN_TTL_S, AccountError,
                       classify_source, hash_password, hash_token,
                       new_token, reset_email_body, send_email,
                       smtp_configured, validate_email, validate_password,
                       verification_email_body, verify_password)
from .ratelimit import RateLimiter

router = APIRouter()

SESSION_COOKIE = 'sf_session'

# limites anti-abus (fenêtre glissante, par IP ou par email)
_signup_limiter = RateLimiter(5, 900)
_login_limiter = RateLimiter(20, 900)
_login_failures = RateLimiter(5, 900)      # par EMAIL
_forgot_limiter = RateLimiter(5, 900)
_track_limiter = RateLimiter(120, 60)
_rotate_limiter = RateLimiter(3, 3600)     # par utilisateur
_resend_limiter = RateLimiter(3, 3600)     # par email


def signup_bonus_credits() -> float:
    """Crédits d'accueil (0 pour désactiver) — mouvement 'topup'
    d'idempotence 'signup-bonus' : jamais compté dans le CA encaissé."""
    try:
        return max(0.0, float(os.environ.get(
            'STATEMENTFLOW_SIGNUP_BONUS_CREDITS', '5')))
    except (TypeError, ValueError):
        return 5.0


# ── helpers ──────────────────────────────────────────────────────────
def _api():
    """Module api (import différé : api.py importe webapp.py)."""
    from . import api as api_mod
    return api_mod


def _pg():
    """PgStore actif ou None (mode JSON : comptes indisponibles)."""
    return _api()._pg_store


def _require_pg():
    store = _pg()
    if store is None:
        raise HTTPException(
            503, 'Comptes indisponibles : cette instance n\'a pas de '
                 'base PostgreSQL configurée (STATEMENTFLOW_DATABASE_URL).')
    return store


def _client_ip(request: Request) -> str:
    fwd = request.headers.get('x-forwarded-for', '')
    if fwd:
        return fwd.split(',')[0].strip()[:64]
    return request.client.host if request.client else 'unknown'


def _secure_cookie(request: Request) -> bool:
    if request.headers.get('x-forwarded-proto') == 'https':
        return True
    return request.url.scheme == 'https'


def _session_user(request: Request) -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE, '')
    if not token:
        return None
    store = _pg()
    if store is None:
        return None
    try:
        return store.get_session_user(hash_token(token))
    except Exception:
        return None


def _require_session(request: Request) -> dict:
    user = _session_user(request)
    if user is None:
        raise HTTPException(401, 'Session expirée : reconnectez-vous.')
    return user


def _require_verified(user: dict) -> None:
    if not user.get('email_verified'):
        raise HTTPException(
            403, 'Adresse email non vérifiée : ouvrez le lien reçu par '
                 'email (ou demandez-en un nouveau depuis le tableau '
                 'de bord).')


def _base_url(request: Request) -> str:
    custom = os.environ.get('STATEMENTFLOW_PUBLIC_BASE_URL', '').strip()
    if custom:
        return custom.rstrip('/')
    return str(request.base_url).rstrip('/')


def _set_session(response: JSONResponse, request: Request,
                 token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_TTL_S,
        httponly=True, samesite='lax', secure=_secure_cookie(request),
        path='/')


def _clear_session(response: JSONResponse) -> None:
    response.delete_cookie(SESSION_COOKIE, path='/')


def record_payment_success_event(record: dict) -> None:
    """Journalise un paiement confirmé (appelé par l'activation, quelle
    que soit la voie : webhook ou polling) — aucune donnée bancaire."""
    store = _pg()
    if store is None:
        return
    try:
        user_id = None
        row = store._call(lambda c: c.execute(
            'select user_id from api_keys where key_hash = %s',
            (record.get('key_hash'),)).fetchone())
        if row and row[0]:
            user_id = str(row[0])
        store.record_event('payment_success', user_id=user_id,
                           metadata={'pack': record.get('pack'),
                                     'amount_fcfa': record.get('amount_fcfa'),
                                     'reference': record.get('reference')})
    except Exception:
        pass


# ── modèles de requêtes ─────────────────────────────────────────────
class SignupRequest(BaseModel):
    email: str
    password: str
    ref: str = ''
    referrer: str = ''
    anonymous_id: str = ''


class LoginRequest(BaseModel):
    email: str
    password: str
    anonymous_id: str = ''


class VerifyRequest(BaseModel):
    token: str


class ForgotRequest(BaseModel):
    email: str


class ResetRequest(BaseModel):
    token: str
    password: str


class TopupRequest(BaseModel):
    pack: str


class RotateRequest(BaseModel):
    confirm: bool = False


class TrackRequest(BaseModel):
    type: str = 'visit'
    path: str = ''
    anonymous_id: str = ''
    source: str = 'direct'
    ref: str = ''
    referrer: str = ''


# ── inscription / connexion ─────────────────────────────────────────
@router.post('/v1/auth/signup')
def auth_signup(body: SignupRequest, request: Request):
    store = _require_pg()
    if not _signup_limiter.allow('ip:' + _client_ip(request)):
        raise HTTPException(429, 'Trop de tentatives : réessayez plus tard.')
    try:
        email = validate_email(body.email)
        password = validate_password(body.password)
    except AccountError as e:
        raise HTTPException(400, str(e))
    source = classify_source(body.referrer, body.ref)
    api_key, key_hash = new_token()
    password_hash, salt_hex = hash_password(password)
    smtp = smtp_configured()
    try:
        store.create_account(
            email, password_hash, salt_hex, key_hash,
            bonus_credits=signup_bonus_credits(),
            verified=not smtp, verified_via=None if smtp else 'auto_no_smtp',
            source=source, ref=(body.ref or '').strip()[:60] or None)
    except Exception as e:
        if 'email_deja_utilise' in str(e):
            raise HTTPException(409, 'Un compte existe déjà avec cet email.')
        raise HTTPException(503, 'Inscription indisponible (base).')
    user = store.get_user_by_email(email)
    store.record_event('signup', anonymous_id=body.anonymous_id,
                       user_id=user['id'], source=source, path='/signup',
                       metadata={'bonus_credits': signup_bonus_credits()})
    resp_body = {
        'status': 'created', 'email': email,
        'credits': signup_bonus_credits(),
        # clé API visible UNE SEULE FOIS (jamais stockée en clair)
        'api_key': api_key,
        'email_verification': 'sent' if smtp else 'not_configured',
    }
    response = JSONResponse(resp_body, status_code=201)
    if not smtp:
        # sans SMTP configuré, le compte est immédiatement utilisable
        # (verified_via='auto_no_smtp' — signalé honnêtement)
        token, token_hash = new_token()
        store.create_session(user['id'], token_hash, SESSION_TTL_S)
        _set_session(response, request, token)
    else:
        vtoken, vhash = new_token()
        store.create_auth_token(user['id'], 'verify_email', vhash,
                                VERIFY_TOKEN_TTL_S)
        link = f'{_base_url(request)}/verify.html?token={vtoken}'
        sent = send_email(email, 'Confirmez votre email StatementFlow',
                          verification_email_body(link))
        if not sent:
            resp_body['email_verification'] = 'send_failed'
            resp_body['hint'] = ('Contactez le support pour activer votre '
                                 'compte.')
    return response


@router.post('/v1/auth/login')
def auth_login(body: LoginRequest, request: Request):
    store = _require_pg()
    ip = _client_ip(request)
    if not _login_limiter.allow('ip:' + ip):
        raise HTTPException(429, 'Trop de tentatives : réessayez plus tard.')
    try:
        email = validate_email(body.email)
    except AccountError:
        raise HTTPException(401, 'Email ou mot de passe incorrect.')
    if not _login_failures.allow('email:' + email):
        raise HTTPException(429, 'Compte temporairement verrouillé '
                                 '(trop d\'échecs) : réessayez plus tard.')
    user = store.get_user_by_email(email)
    if user is None or not verify_password(
            body.password, user['password_hash'], user['password_salt']):
        raise HTTPException(401, 'Email ou mot de passe incorrect.')
    _login_failures.reset('email:' + email)
    token, token_hash = new_token()
    store.create_session(user['id'], token_hash, SESSION_TTL_S)
    store.touch_login(user['id'])
    store.record_event('login', user_id=user['id'], path='/login')
    response = JSONResponse({'status': 'ok', 'email': email})
    _set_session(response, request, token)
    return response


@router.post('/v1/auth/logout')
def auth_logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE, '')
    store = _pg()
    if store is not None and token:
        try:
            store.delete_session(hash_token(token))
        except Exception:
            pass
    response = JSONResponse({'status': 'ok'})
    _clear_session(response)
    return response


@router.get('/v1/auth/session')
def auth_session(request: Request):
    user = _session_user(request)
    if user is None:
        return {'authenticated': False}
    store = _pg()
    credits = None
    key_masked = None
    if store is not None:
        try:
            key_hash = store.user_active_key_hash(user['id'])
            if key_hash:
                key_masked = key_hash[:6] + '…'
                rec = store.lookup(key_hash)
                credits = rec['credits'] if rec else None
        except Exception:
            pass
    return {'authenticated': True, 'email': user['email'],
            'email_verified': user['email_verified'],
            'verified_via': user['verified_via'],
            'credits': credits, 'api_key_masked': key_masked}


# ── vérification email / mot de passe oublié ────────────────────────
@router.post('/v1/auth/verify')
def auth_verify(body: VerifyRequest):
    store = _require_pg()
    user_id = store.consume_auth_token('verify_email',
                                       hash_token(body.token.strip()))
    if user_id is None:
        raise HTTPException(400, 'Lien invalide ou expiré : demandez-en '
                                 'un nouveau.')
    store.mark_email_verified(user_id, via='smtp')
    return {'status': 'verified'}


@router.post('/v1/auth/verify/resend')
def auth_verify_resend(body: ForgotRequest, request: Request):
    store = _require_pg()
    if not _resend_limiter.allow('email:' + _client_ip(request)):
        raise HTTPException(429, 'Trop de demandes : réessayez plus tard.')
    if not smtp_configured():
        raise HTTPException(400, 'La vérification par email n\'est pas '
                                 'configurée sur cette instance.')
    try:
        email = validate_email(body.email)
    except AccountError as e:
        raise HTTPException(400, str(e))
    user = store.get_user_by_email(email)
    # réponse identique que le compte existe ou non (pas d'énumération)
    if user is not None and not user['email_verified']:
        vtoken, vhash = new_token()
        store.create_auth_token(user['id'], 'verify_email', vhash,
                                VERIFY_TOKEN_TTL_S)
        link = f'{_base_url(request)}/verify.html?token={vtoken}'
        send_email(email, 'Confirmez votre email StatementFlow',
                   verification_email_body(link))
    return {'status': 'sent'}


@router.post('/v1/auth/password/forgot')
def auth_password_forgot(body: ForgotRequest, request: Request):
    store = _require_pg()
    if not _forgot_limiter.allow('ip:' + _client_ip(request)):
        raise HTTPException(429, 'Trop de demandes : réessayez plus tard.')
    try:
        email = validate_email(body.email)
    except AccountError:
        email = ''
    user = store.get_user_by_email(email) if email else None
    if user is not None:
        rtoken, rhash = new_token()
        store.create_auth_token(user['id'], 'password_reset', rhash,
                                RESET_TOKEN_TTL_S)
        link = f'{_base_url(request)}/reset.html?token={rtoken}'
        send_email(email, 'Réinitialisation de votre mot de passe '
                          'StatementFlow', reset_email_body(link))
    # toujours 200 : aucune énumération de comptes
    return {'status': 'sent'}


@router.post('/v1/auth/password/reset')
def auth_password_reset(body: ResetRequest):
    store = _require_pg()
    try:
        password = validate_password(body.password)
    except AccountError as e:
        raise HTTPException(400, str(e))
    user_id = store.consume_auth_token('password_reset',
                                       hash_token(body.token.strip()))
    if user_id is None:
        raise HTTPException(400, 'Lien invalide ou expiré : refaites une '
                                 'demande de réinitialisation.')
    password_hash, salt_hex = hash_password(password)
    store._call(lambda c: c.execute(
        'update users set password_hash = %s, password_salt = %s'
        ' where id = %s::uuid', (password_hash, salt_hex, user_id)))
    # sécurité : toutes les sessions du compte sont fermées
    store._call(lambda c: c.execute(
        'delete from sessions where user_id = %s::uuid', (user_id,)))
    return {'status': 'reset'}


# ── tableau de bord ─────────────────────────────────────────────────
@router.get('/v1/account/summary')
def account_summary(request: Request):
    user = _require_session(request)
    store = _require_pg()
    key_hash = store.user_active_key_hash(user['id'])
    credits = None
    if key_hash:
        rec = store.lookup(key_hash)
        credits = rec['credits'] if rec else None
    conversions = store.list_jobs_for_user(user['id'], limit=5)
    done = [c for c in conversions if c['status'] == 'done']
    return {
        'email': user['email'],
        'email_verified': user['email_verified'],
        'verified_via': user['verified_via'],
        'credits': credits,
        'api_key_masked': (key_hash[:6] + '…') if key_hash else None,
        'recent_conversions': len(conversions),
        'total_transactions': sum(c['transactions'] or 0 for c in done),
        'smtp_configured': smtp_configured(),
        'version': __version__,
    }


@router.get('/v1/account/conversions')
def account_conversions(request: Request, limit: int = 20,
                        offset: int = 0):
    user = _require_session(request)
    store = _require_pg()
    limit = max(1, min(int(limit), 100))
    return {'records': store.list_jobs_for_user(
        user['id'], limit=limit, offset=max(0, int(offset)))}


@router.get('/v1/account/topups')
def account_topups(request: Request):
    user = _require_session(request)
    store = _require_pg()
    return {'records': store.list_topups_for_user(user['id'], limit=50)}


@router.post('/v1/account/key/rotate')
def account_key_rotate(body: RotateRequest, request: Request):
    """Régénère la clé API du compte : l'ancienne est RÉVOQUÉE et le
    solde transféré (transaction SQL). La nouvelle clé n'est visible
    qu'UNE fois, dans cette réponse."""
    user = _require_session(request)
    store = _require_pg()
    if not body.confirm:
        raise HTTPException(400, 'Confirmation requise (confirm: true) — '
                                 'l\'ancienne clé cessera immédiatement '
                                 'de fonctionner.')
    if not _rotate_limiter.allow('user:' + user['id']):
        raise HTTPException(429, 'Trop de régénérations : réessayez '
                                 'dans une heure.')
    new_key, new_hash = new_token()
    balance = store.rotate_key(user['id'], new_hash)
    return {'status': 'rotated', 'api_key': new_key,
            'credits_transferred': round(balance, 2)}


# ── conversion depuis le tableau de bord ────────────────────────────
@router.post('/v1/account/convert', status_code=202)
async def account_convert(request: Request, file: UploadFile = File(...)):
    """Upload depuis le tableau de bord : MÊME moteur et MÊME chemin
    financier que POST /v1/analyze (reserve → settle/refund par job_id
    idempotent), mais authentifié par la session du compte. Les crédits
    débités sont ceux du grand livre PostgreSQL du compte."""
    import time as _time
    import uuid as _uuid

    from .billing import credits_for
    from .engine import ParseError, parse_bytes

    user = _require_session(request)
    _require_verified(user)
    store = _require_pg()
    api_mod = _api()

    key_hash = store.user_active_key_hash(user['id'])
    if not key_hash:
        raise HTTPException(500, 'Aucune clé active sur ce compte.')
    data = await api_mod._read_upload_capped(
        file, int(api_mod.config.max_file_mb * 1024 * 1024))
    if not data:
        raise HTTPException(400, 'Fichier vide')
    api_mod._sweep_jobs()
    managed = api_mod.managed_keys.lookup(key_hash)
    if managed is None:
        raise HTTPException(500, 'Compte sans clé gérée : contactez le '
                                 'support.')
    from .security import sanitize_filename
    filename = sanitize_filename(file.filename)
    job_id = _uuid.uuid4().hex
    # réservation atomique d'un crédit (le coût réel est complété après
    # le traitement ; un échec restitue tout)
    if not api_mod.managed_keys.reserve(key_hash, 1.0, job_id=job_id):
        raise HTTPException(402, 'Crédits épuisés : rechargez depuis le '
                                 'tableau de bord.')
    api_mod._jobs[job_id] = {'status': 'processing',
                             'created': _time.time(),
                             'key_hash': key_hash}
    store.record_event('conversion_started', user_id=user['id'],
                       path='/dashboard',
                       metadata={'kind': api_mod.detect_kind_hint(filename)})
    try:
        api_mod.managed_keys.record_job(
            job_id, key_hash, filename,
            api_mod.detect_kind_hint(filename), 'processing')
    except Exception:
        pass
    try:
        result = await api_mod.run_in_threadpool(
            parse_bytes, data, filename, api_mod.config)
        api_mod.usage_store.record(key_hash,
                                   result['usage']['pages'],
                                   result['usage']['transactions'])
        cost = credits_for(result['usage'], result['document']['kind'])
        remaining = api_mod.managed_keys.settle(key_hash, cost - 1.0,
                                                job_id=job_id)
        result['billing'] = {
            'credits_charged': cost,
            'credits_remaining': round(remaining, 2)
            if remaining is not None and remaining >= 0 else None,
            'billing_mode': 'managed',
        }
        api_mod._jobs[job_id].update(status='done', result=result)
        api_mod._record_job_done(job_id, key_hash, filename, result)
        store.record_event('conversion_done', user_id=user['id'],
                           metadata={'transactions':
                                     result['usage']['transactions'],
                                     'credits': cost})
    except (ParseError, Exception) as e:
        api_mod.managed_keys.refund(key_hash, 1.0, job_id=job_id)
        api_mod._jobs[job_id].update(status='error',
                                     error=str(e)[:500])
        try:
            api_mod.managed_keys.record_job(
                job_id, key_hash, filename,
                api_mod.detect_kind_hint(filename), 'error',
                error=str(e)[:500])
        except Exception:
            pass
    return {'job_id': job_id, 'status': api_mod._jobs[job_id]['status'],
            'poll': f'/v1/account/result/{job_id}'}


@router.get('/v1/account/result/{job_id}')
def account_result(job_id: str, request: Request):
    user = _require_session(request)
    store = _require_pg()
    api_mod = _api()
    api_mod._sweep_jobs()
    # mémoire d'abord, puis PostgreSQL (v0.6.1) : le résultat reste
    # accessible pendant son TTL même après un redémarrage du service
    job = api_mod._jobs.get(job_id)
    if job is not None:
        if not store.user_owns_key_hash(user['id'], job['key_hash']):
            job = None
    else:
        try:
            rec = api_mod._pg_store.get_job_for_user(user['id'], job_id)
        except Exception:
            rec = None
        if rec is not None:
            job = {'status': rec['status'], 'result': rec['result'],
                   'error': rec['error']}
    # 404 identique pour job inconnu et job d'un AUTRE compte
    if job is None:
        raise HTTPException(404, 'Résultat inconnu, expiré ou non '
                                 'accessible depuis ce compte.')
    if job['status'] == 'processing':
        return JSONResponse({'job_id': job_id, 'status': 'processing'},
                            status_code=202)
    if job['status'] == 'error':
        return JSONResponse(
            {'job_id': job_id, 'status': 'error', 'error': job['error']},
            status_code=422)
    return {'job_id': job_id, 'status': 'done', **job['result']}


# ── achats (montants TOUJOURS issus du catalogue serveur) ───────────
@router.post('/v1/account/topup')
def account_topup(body: TopupRequest, request: Request):
    from .billing import SasPayError
    user = _require_session(request)
    _require_verified(user)
    store = _require_pg()
    api_mod = _api()
    pack = (body.pack or '').strip().lower()
    if pack not in api_mod.CREDIT_PACKS:
        raise HTTPException(400, f'Pack inconnu « {pack} ».')
    key_hash = store.user_active_key_hash(user['id'])
    if not key_hash:
        raise HTTPException(500, 'Aucune clé active sur ce compte.')
    try:
        session = api_mod.saspay.create_checkout(
            amount_fcfa=api_mod.CREDIT_PACKS[pack]['amount_fcfa'],
            description=f"StatementFlow {api_mod.CREDIT_PACKS[pack]['label']}",
            customer_email=user['email'],
            customer_name=user['email'].split('@')[0][:60],
            metadata={'product': 'statementflow', 'pack': pack})
    except SasPayError as e:
        raise HTTPException(502, f'Passerelle de paiement indisponible : '
                                 f'{e}')
    record = api_mod.billing_store.create_topup(key_hash, pack, session)
    store.record_event('topup_created', user_id=user['id'],
                       metadata={'pack': pack})
    return {
        'reference': record['reference'], 'pack': pack,
        'credits': record['credits'],
        'amount_fcfa': record['amount_fcfa'],
        'status': record['status'],
        'checkout_url': session.get('checkout_url', ''),
        'note': 'Crédits activés après confirmation du paiement '
                '(webhook signé + vérification serveur).',
    }


@router.get('/v1/account/topup/{reference}')
def account_topup_status(reference: str, request: Request):
    """Polling du statut : interroge l'état RÉEL chez le prestataire et
    active les crédits si le paiement est confirmé (le webhook fait de
    même de son côté — idempotence par transaction_id unique)."""
    from .billing import is_failed, is_paid
    user = _require_session(request)
    store = _require_pg()
    api_mod = _api()
    record = store.get_topup_for_user(reference, user['id'])
    if record is None:
        raise HTTPException(404, 'Rechargement inconnu.')
    if record['status'] == 'pending':
        try:
            status = api_mod.saspay.checkout_status(record['session_id'])
        except Exception:
            raise HTTPException(502, 'Passerelle injoignable : '
                                     'réessayez dans un instant.')
        if is_paid(status):
            api_mod._activate_topup(record,
                                    status.get('transaction_id') or '')
            record = store.get_topup_for_user(reference, user['id'])
        elif is_failed(status):
            api_mod.billing_store.mark_failed(reference)
            record = store.get_topup_for_user(reference, user['id'])
    credits = None
    key_hash = store.user_active_key_hash(user['id'])
    if key_hash:
        rec = store.lookup(key_hash)
        credits = rec['credits'] if rec else None
    return {'reference': reference, 'status': record['status'],
            'credits': record['credits'],
            'transaction_id': record['transaction_id'],
            'amount_fcfa': record['amount_fcfa'],
            'balance': credits}


# ── tracking public (visites) ───────────────────────────────────────
@router.post('/v1/track')
def track(body: TrackRequest, request: Request):
    if not _track_limiter.allow('ip:' + _client_ip(request)):
        raise HTTPException(429)
    if body.type != 'visit':
        raise HTTPException(400, 'Seul le type « visit » est accepté.')
    store = _pg()
    if store is None:
        return {'status': 'ignored'}
    user = _session_user(request)
    store.record_event(
        'visit', anonymous_id=body.anonymous_id,
        user_id=user['id'] if user else None,
        source=classify_source(body.referrer, body.ref),
        path=(body.path or '/')[:200])
    return {'status': 'ok'}


# ── statistiques administrateur ─────────────────────────────────────
@router.get('/v1/admin/stats')
def admin_stats(request: Request,
                x_admin_key: Optional[str] = Header(default=None,
                                                    alias='X-Admin-Key')):
    api_mod = _api()
    if not api_mod.admin_auth.configured \
            or not api_mod.admin_auth.verify(x_admin_key):
        raise HTTPException(403, 'Statistiques d\'administration : clé '
                                 'd\'administration requise.')
    store = _pg()
    if store is None:
        raise HTTPException(503, 'Statistiques indisponibles sans '
                                 'PostgreSQL.')
    return store.admin_stats()


# ── démo publique (moteur RÉEL, sans compte, sans débit) ───────────
_demo_limiter = RateLimiter(6, 3600)      # 6 fichiers/heure par IP
_DEMO_MAX_BYTES = 2 * 1024 * 1024         # 2 Mo pour la démo


@router.post('/v1/demo/analyze')
async def demo_analyze(request: Request, file: UploadFile = File(...)):
    """Démo publique : MÊME moteur de conversion que les comptes, sans
    clé ni compte. Limites strictes (2 Mo, 6 essais/heure/IP, résultat
    tronqué à 25 transactions). Le fichier n'est ni stocké ni journalisé
    — traitement éphémère, exactement comme en production."""
    from .billing import credits_for
    from .engine import parse_bytes
    from .security import sanitize_filename
    if not _demo_limiter.allow('ip:' + _client_ip(request)):
        raise HTTPException(429, 'Limite de la démo atteinte (6 fichiers/'
                                 'heure) : créez un compte pour convertir '
                                 'sans contrainte.')
    data = await _api()._read_upload_capped(file, _DEMO_MAX_BYTES)
    if not data:
        raise HTTPException(400, 'Fichier vide')
    filename = sanitize_filename(file.filename) or 'document'
    try:
        result = await _api().run_in_threadpool(
            parse_bytes, data, filename, _api().config)
    except Exception as e:
        raise HTTPException(422, f'Analyse impossible : {str(e)[:200]}')
    preview_text = data[:1200].decode('utf-8', errors='replace')
    preview_lines = preview_text.splitlines()[:25]
    truncated_result = dict(result)
    truncated_result['transactions'] = result.get('transactions', [])[:25]
    if len(result.get('transactions', [])) > 25:
        truncated_result['note'] = (
            'Démo : 25 premières transactions sur '
            f"{len(result['transactions'])} — créez un compte pour le "
            'résultat complet.')
    truncated_result['billing'] = {
        'info': 'démo : aucun crédit débité',
        'credits_equivalent': credits_for(
            result['usage'], result['document']['kind']),
    }
    return {'input_preview': '\n'.join(preview_lines),
            'result': truncated_result}


# ── frontend statique (appelé par api.py APRÈS le routeur) ─────────
class _WebFiles(StaticFiles):
    """StaticFiles qui ne répond QUE pour les fichiers existants :
    un chemin inconnu reste 404 (le mount '/' ne doit pas transformer
    les 404 de l'API en 405 pour les méthodes non-GET)."""

    async def __call__(self, scope, receive, send):
        from starlette.exceptions import HTTPException as _StarletteHTTP
        try:
            await super().__call__(scope, receive, send)
        except _StarletteHTTP:
            from starlette.responses import JSONResponse
            response = JSONResponse(
                {'error': {'code': 'not_found',
                           'message': 'Ressource inconnue.'}},
                status_code=404)
            await response(scope, receive, send)


def mount_webapp(app) -> None:
    """Sert web/ (landing, dashboard, PWA) en dernier recours : les
    routes /v1/* et /docs restent prioritaires. Répertoire résolu :
    STATEMENTFLOW_WEB_DIR, puis ./web (déploiement Docker et dépôt)."""
    web_dir = os.environ.get('STATEMENTFLOW_WEB_DIR', '').strip()
    if not web_dir or not os.path.isdir(web_dir):
        web_dir = os.path.join(os.getcwd(), 'web')
    if not os.path.isdir(web_dir):
        return  # pas de frontend embarqué (image API seule)
    app.mount('/', _WebFiles(directory=web_dir, html=True), name='web')
