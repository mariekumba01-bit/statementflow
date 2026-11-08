"""Facturation prépayée : crédits, rechargements, intégration SasPay.

Modèle (v0.3.0, voir docs/PRICING.md) : prépaiement par packs de crédits.
1 crédit ≈ 1 page PDF traitée, ou 50 lignes d'un document tabulaire
(CSV/TSV/XLSX). Les crédits sont activés UNIQUEMENT après confirmation
serveur du paiement — webhook signé vérifié + statut réel interrogé chez
le prestataire — jamais sur une simple redirection navigateur.

Intégration SasPay (docs.saspay.me, contrats vérifiés le 2026-11-08) :
- POST /checkout-sessions/          → session {id, checkout_url, PENDING} ;
- GET  /checkout-sessions/{id}/status/ → {status: PAID, transaction_id,
  transaction_status: SUCCESS, ...} (revérifie toujours l'état gateway) ;
- GET  /payments/{payment_id}/verify/  → objet transaction (statut serveur) ;
- webhook « transaction.success » : headers X-Webhook-Signature (HMAC-SHA256
  hex du corps « {timestamp}.{body} »), X-Webhook-Timestamp (unix secondes),
  X-Webhook-Event ; tolérance 300 s ; 5 tentatives de renvoi ;
- authentification : Authorization: Bearer sk_test_…/sk_live_…,
  base https://api.saspay.me/api/v1 ;
- frais affichés sur saspay.me (consulté 2026-11-08) : pas d'abonnement,
  à partir de 1,75 % + 100 FCFA fixe par transaction.

Mode test : sans STATEMENTFLOW_SASPAY_SECRET_KEY, un FakeSasPay local
simule la passerelle avec les MÊMES contrats (création de session, statut,
vérification, signature de webhooks selon le schéma documenté). Le
parcours complet — achat → webhook → activation — est ainsi testable sans
compte marchand ; aucun paiement réel n'est possible en mode test.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import threading
import time
import uuid
from typing import Optional

ENV_PREFIX = 'STATEMENTFLOW_'

# Packs vendus (montants en FCFA ; cohérence garantie par test unitaire :
# prix décroissant par crédit quand le volume augmente)
CREDIT_PACKS = {
    'starter': {'credits': 500, 'amount_fcfa': 5000,
                'label': 'Starter — 500 crédits'},
    'pro': {'credits': 2500, 'amount_fcfa': 20000,
            'label': 'Pro — 2 500 crédits'},
    'business': {'credits': 10000, 'amount_fcfa': 60000,
                 'label': 'Business — 10 000 crédits'},
}

TABULAR_ROWS_PER_CREDIT = 50  # 1 crédit ≈ 1 page PDF ou 50 lignes CSV/XLSX

WEBHOOK_TOLERANCE_S = 300  # tolérance documentée SasPay (5 minutes)

# statuts de session/paiement considérés comme payés (défensif : la doc ne
# liste que PAID/SUCCESS ; d'autres variantes restent interprétables)
_PAID_STATUSES = {'PAID', 'SUCCESS', 'COMPLETED', 'SUCCEEDED'}
_FAILED_STATUSES = {'FAILED', 'CANCELLED', 'CANCELED', 'EXPIRED'}


def credits_for(usage: dict, kind: str) -> int:
    """Crédits consommés par un traitement (règle publique documentée).

    PDF : 1 crédit par page ; CSV/TSV/XLSX : 1 crédit par tranche de
    50 lignes (minimum 1). Jamais zéro : un traitement consomme au moins
    1 crédit.
    """
    if kind == 'pdf':
        return max(1, int(usage.get('pages') or 1))
    # lignes RÉELLES du document (v0.4.0) ; repli sur le nombre de
    # transactions pour les résultats antérieurs qui n'exposaient pas rows
    rows = usage.get('rows') or usage.get('transactions') or 1
    return max(1, math.ceil(rows / TABULAR_ROWS_PER_CREDIT))


# ---------------------------------------------------------------------------
# Signature webhook (schéma documenté SasPay, cf. docs.saspay.me/webhooks)
# ---------------------------------------------------------------------------
def sign_webhook(secret: str, timestamp: int, body: bytes) -> str:
    """HMAC-SHA256 hex de « {timestamp}.{body} » (mode test / outillage)."""
    signed = f'{timestamp}.'.encode() + body
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def verify_webhook_signature(raw_body: bytes, timestamp: str,
                             signature: str, secret: str,
                             tolerance_s: int = WEBHOOK_TOLERANCE_S) -> bool:
    """Vérifie un webhook SasPay : âge du timestamp + signature HMAC
    (comparaison en temps constant). Le corps DOIT être brut (jamais
    re-sérialisé)."""
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - ts) > tolerance_s:
        return False
    expected = sign_webhook(secret, ts, raw_body)
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Client SasPay (production) — contrats de docs.saspay.me
# ---------------------------------------------------------------------------
class SasPayError(RuntimeError):
    """Erreur d'appel SasPay (réseau, HTTP, métier). Le message ne doit
    contenir aucun secret (clé Bearer jamais incluse)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class SasPayClient:
    """Client HTTP minimal (stdlib) de l'API SasPay."""

    def __init__(self, secret_key: str, base_url: str = '',
                 timeout: int = 30):
        self._key = secret_key
        self._base = (base_url or 'https://api.saspay.me/api/v1').rstrip('/')
        self._timeout = timeout

    def _request(self, method: str, path: str,
                 body: Optional[dict] = None) -> dict:
        import urllib.error
        import urllib.request
        url = self._base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header('Authorization', f'Bearer {self._key}')
        if data is not None:
            req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:200]
            raise SasPayError(
                f'SasPay HTTP {e.code} sur {path} : {detail}', e.code) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SasPayError(
                f'SasPay injoignable ({path}) : {type(e).__name__}') from None
        # enveloppe documentée : {success, data, code}
        if isinstance(payload, dict) and 'data' in payload:
            return payload['data'] if payload.get('success', True) else {}
        return payload

    def create_checkout(self, amount_fcfa: int, description: str,
                        customer_email: str, customer_name: str,
                        metadata: Optional[dict] = None,
                        return_url: str = '', country: str = 'SN',
                        currency: str = 'XOF') -> dict:
        """POST /checkout-sessions/ → session (id, checkout_url, PENDING)."""
        body = {
            'amount': f'{float(amount_fcfa):.2f}',
            'currency': currency,
            'customer_email': customer_email,
            'customer_name': customer_name,
            'description': description,
            'country': country,
        }
        if metadata:
            body['metadata'] = metadata
        if return_url:
            body['return_url'] = return_url
        return self._request('POST', '/checkout-sessions/', body)

    def checkout_status(self, session_id: str) -> dict:
        """GET /checkout-sessions/{id}/status/ → {status, transaction_id,
        transaction_status, ...} (état réel gateway, jamais en cache)."""
        return self._request(
            'GET', f'/checkout-sessions/{session_id}/status/')

    def verify_payment(self, payment_id: str) -> dict:
        """GET /payments/{id}/verify/ → objet transaction (statut serveur)."""
        return self._request('GET', f'/payments/{payment_id}/verify/')


# ---------------------------------------------------------------------------
# Simulateur local (mode test) — mêmes contrats, aucune connexion
# ---------------------------------------------------------------------------
class FakeSasPay:
    """Passerelle factice pour le mode test et les tests automatisés.

    Reproduit les contrats documentés : création de session (201-like),
    statut plat {status, transaction_id, transaction_status}, vérification
    de paiement, et webhooks signés selon le schéma HMAC officiel. Le
    paiement est simulé explicitement via pay_session()."""

    def __init__(self, webhook_secret: str = 'test-webhook-secret'):
        self.webhook_secret = webhook_secret
        self._sessions: dict[str, dict] = {}
        self._transactions: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create_checkout(self, amount_fcfa: int, description: str,
                        customer_email: str, customer_name: str,
                        metadata: Optional[dict] = None,
                        return_url: str = '', country: str = 'SN',
                        currency: str = 'XOF') -> dict:
        sid = str(uuid.uuid4())
        session = {
            'id': sid,
            'checkout_url': f'https://pay.saspay.test/checkout/{sid}',
            'amount': f'{float(amount_fcfa):.2f}',
            'currency': currency,
            'description': description,
            'metadata': metadata or {},
            'status': 'PENDING',
            'transaction': None,
            'paid_at': None,
        }
        with self._lock:
            self._sessions[sid] = session
        return dict(session)

    def pay_session(self, session_id: str, success: bool = True
                    ) -> Optional[dict]:
        """Simule le paiement d'une session (déclencheur du webhook)."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session['status'] != 'PENDING':
                return None
            tx_id = str(uuid.uuid4())
            status = 'SUCCESS' if success else 'FAILED'
            session['status'] = 'PAID' if success else 'FAILED'
            session['transaction'] = tx_id
            session['paid_at'] = time.strftime(
                '%Y-%m-%dT%H:%M:%SZ', time.gmtime()) if success else None
            tx = {
                'id': tx_id,
                'reference': f'TXN-TEST-{tx_id[:8].upper()}',
                'status': status,
                'amount': session['amount'],
                'currency': session['currency'],
                'net_amount': session['amount'],
                'description': session['description'],
                'metadata': session['metadata'],
            }
            self._transactions[tx_id] = tx
            return self.build_webhook(
                'transaction.success' if success else 'transaction.failed',
                dict(tx))

    def checkout_status(self, session_id: str) -> dict:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SasPayError('Session introuvable (mode test)', 404)
            return {
                'id': session['id'],
                'slug': session_id[:8],
                'status': session['status'],
                'transaction_id': session['transaction'],
                'transaction_status': (self._transactions.get(
                    session['transaction'] or '', {}).get('status')
                    if session['transaction'] else None),
                'transaction_reference': session['transaction'],
            }

    def verify_payment(self, payment_id: str) -> dict:
        with self._lock:
            tx = self._transactions.get(payment_id)
            if tx is None:
                raise SasPayError('Transaction introuvable (mode test)', 404)
            return dict(tx)

    def build_webhook(self, event: str, data: dict
                      ) -> tuple[bytes, str, str, str]:
        """Construit (body, timestamp, signature, event) selon le schéma
        documenté — utilisé par les tests pour valider la vraie vérification."""
        body = json.dumps({'event': event, 'data': data}).encode()
        ts = str(int(time.time()))
        return body, ts, sign_webhook(self.webhook_secret, int(ts), body), event


def provider_from_env() -> tuple[object, str]:
    """(provider, mode) selon l'environnement : SasPayClient si une clé
    secrète est configurée, sinon FakeSasPay (mode test). Le simulateur
    SIGNE ses webhooks avec STATEMENTFLOW_SASPAY_WEBHOOK_SECRET lorsqu'il
    est défini — cohérence avec la vérification du routeur webhook."""
    key = os.environ.get(ENV_PREFIX + 'SASPAY_SECRET_KEY', '')
    if key:
        base = os.environ.get(ENV_PREFIX + 'SASPAY_API_URL', '')
        return SasPayClient(key, base_url=base), 'saspay'
    whsec = os.environ.get(ENV_PREFIX + 'SASPAY_WEBHOOK_SECRET', '')
    return FakeSasPay(webhook_secret=whsec or 'test-webhook-secret'), 'test'


# ---------------------------------------------------------------------------
# Magasin de rechargements (JSON, thread-safe, idempotent)
# ---------------------------------------------------------------------------
class BillingStore:
    """Rechargements prépayés + idempotence des webhooks, persistés en JSON.

    Enregistrements : {reference: {session_id, key_hash, pack, credits,
    amount_fcfa, status (pending|activated|failed|refunded),
    transaction_id, created, activated_at}}. La clé d'idempotence webhook
    est « event:transaction_id » — un événement rejoué n'active jamais deux
    fois les crédits.
    """

    def __init__(self, path: str = ''):
        self._path = path
        self._lock = threading.Lock()
        self._topups: dict[str, dict] = {}
        self._webhook_events: dict[str, float] = {}
        self._load()

    def _load(self):
        if self._path and os.path.exists(self._path):
            try:
                with open(self._path, encoding='utf-8') as f:
                    data = json.load(f)
                self._topups = data.get('topups', {})
                self._webhook_events = data.get('webhook_events', {})
            except (json.JSONDecodeError, OSError):
                self._topups, self._webhook_events = {}, {}

    def _flush(self):
        if not self._path:
            return
        tmp = self._path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'topups': self._topups,
                       'webhook_events': self._webhook_events}, f)
        os.replace(tmp, self._path)

    def create_topup(self, key_hash: str, pack: str, session: dict
                     ) -> dict:
        ref = 'TOPUP-' + uuid.uuid4().hex[:12].upper()
        record = {
            'reference': ref,
            'session_id': session.get('id', ''),
            'checkout_url': session.get('checkout_url', ''),
            'key_hash': key_hash,
            'pack': pack,
            'credits': CREDIT_PACKS[pack]['credits'],
            'amount_fcfa': CREDIT_PACKS[pack]['amount_fcfa'],
            'status': 'pending',
            'transaction_id': None,
            'created': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'activated_at': None,
        }
        with self._lock:
            self._topups[ref] = record
            self._flush()
        return dict(record)

    def get(self, ref: str) -> Optional[dict]:
        with self._lock:
            t = self._topups.get(ref)
            return dict(t) if t else None

    def pending(self) -> list[dict]:
        with self._lock:
            return [dict(t) for t in self._topups.values()
                    if t['status'] == 'pending']

    def activate(self, ref: str, transaction_id: str) -> Optional[dict]:
        """Marque le rechargement activé (idempotent : un rechargement déjà
        activé/remboursé ne l'est jamais deux fois)."""
        with self._lock:
            t = self._topups.get(ref)
            if t is None or t['status'] != 'pending':
                return None
            t['status'] = 'activated'
            t['transaction_id'] = transaction_id
            t['activated_at'] = time.strftime(
                '%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            self._flush()
            return dict(t)

    def mark_failed(self, ref: str, transaction_id: str = ''):
        with self._lock:
            t = self._topups.get(ref)
            if t is not None and t['status'] == 'pending':
                t['status'] = 'failed'
                t['transaction_id'] = transaction_id or t['transaction_id']
                self._flush()

    def mark_refunded(self, ref: str) -> Optional[dict]:
        with self._lock:
            t = self._topups.get(ref)
            if t is None or t['status'] != 'activated':
                return None
            t['status'] = 'refunded'
            self._flush()
            return dict(t)

    def webhook_seen(self, event_key: str) -> bool:
        with self._lock:
            return event_key in self._webhook_events

    def mark_webhook(self, event_key: str):
        with self._lock:
            self._webhook_events[event_key] = time.time()
            # purge : ne garder que 30 jours d'événements (anti-croissance)
            cutoff = time.time() - 30 * 86400
            stale = [k for k, ts in self._webhook_events.items() if ts < cutoff]
            for k in stale:
                del self._webhook_events[k]
            self._flush()


def is_paid(status_response: dict) -> bool:
    """Interprète une réponse de statut (session ou transaction) : payé ?"""
    session_status = str(status_response.get('status') or '').upper()
    tx_status = str(status_response.get('transaction_status')
                    or status_response.get('status') or '').upper()
    return (session_status in _PAID_STATUSES
            or tx_status in _PAID_STATUSES)


def is_failed(status_response: dict) -> bool:
    session_status = str(status_response.get('status') or '').upper()
    tx_status = str(status_response.get('transaction_status')
                    or status_response.get('status') or '').upper()
    return session_status in _FAILED_STATUSES or tx_status in _FAILED_STATUSES
