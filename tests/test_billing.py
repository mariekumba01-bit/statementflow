"""Tests de la facturation prépayée v0.3.0 : crédits, packs, achat,
webhook SasPay signé, idempotence, échecs, isolation par clé."""
import io
import json
import time
import uuid

import pytest

fastapi = pytest.importorskip('fastapi')
from fastapi.testclient import TestClient  # noqa: E402

from statementflow import api as api_mod  # noqa: E402
from statementflow.billing import (CREDIT_PACKS, FakeSasPay,  # noqa: E402
                                   SasPayError, credits_for,
                                   sign_webhook, verify_webhook_signature)
from statementflow.security import (ApiKeyStore, ManagedKeyStore,  # noqa: E402
                                    hash_key)

KEY = 'sk-billing-test'
CSV_BYTES = (b'Date;Libelle;Debit;Credit;Solde\n'
             b'01/02/2026;A;100;;900\n'
             b'02/02/2026;B;;50;950\n')
WEBHOOK_SECRET = 'whsec-test'


@pytest.fixture()
def billing_env(monkeypatch, tmp_path):
    """API en mode facturation gérée : clés en fichier (persistance
    réelle sur tmp_path), simulateur de passerelle, secret webhook."""
    keys_path = tmp_path / 'keys.json'
    billing_path = tmp_path / 'billing.json'
    store = ManagedKeyStore(str(keys_path))
    key, record = store.issue('client-test', credits=3)
    monkeypatch.setattr(api_mod, 'managed_keys',
                        ManagedKeyStore(str(keys_path)))
    monkeypatch.setattr(api_mod, 'billing_store',
                        api_mod.BillingStore(str(billing_path)))
    gateway = FakeSasPay(webhook_secret=WEBHOOK_SECRET)
    monkeypatch.setattr(api_mod, 'saspay', gateway)
    monkeypatch.setenv('STATEMENTFLOW_SASPAY_WEBHOOK_SECRET', WEBHOOK_SECRET)
    api_mod._jobs.clear()
    client = TestClient(api_mod.app)
    return client, key, gateway, store


def _analyze(client, key, data=CSV_BYTES, filename='doc.csv'):
    return client.post('/v1/analyze',
                       files={'file': (filename, io.BytesIO(data))},
                       headers={'X-API-Key': key})


def _result(client, key, job_id):
    return client.get(f'/v1/results/{job_id}',
                      headers={'X-API-Key': key})


class TestCreditsEngine:
    def test_regle_de_comptage(self):
        assert credits_for({'pages': 5}, 'pdf') == 5
        assert credits_for({'rows': 200}, 'xlsx') == 4  # 4 × 50
        assert credits_for({'rows': 1}, 'csv') == 1
        assert credits_for({}, 'pdf') == 1

    def test_packs_degressifs(self):
        per = [CREDIT_PACKS[p]['amount_fcfa'] / CREDIT_PACKS[p]['credits']
               for p in ('starter', 'pro', 'business')]
        assert per[0] > per[1] > per[2]

    def test_signature_webhook(self):
        body = b'{"event":"transaction.success","data":{"id":"x"}}'
        ts = str(int(time.time()))
        sig = sign_webhook(WEBHOOK_SECRET, int(ts), body)
        assert verify_webhook_signature(body, ts, sig, WEBHOOK_SECRET)
        assert not verify_webhook_signature(body, ts, sig, 'autre-secret')
        assert not verify_webhook_signature(body + b'!', ts, sig,
                                            WEBHOOK_SECRET)
        assert not verify_webhook_signature(body, str(int(time.time()) - 400),
                                            sig, WEBHOOK_SECRET)


class TestAnalyzeBilling:
    def test_decompte_apres_traitement_reussi(self, billing_env):
        client, key, _, store = billing_env
        kh = hash_key(key)
        r = _analyze(client, key)
        assert r.status_code == 202
        job = r.json()['job_id']
        res = _result(client, key, job).json()
        # CSV 2 lignes de données + entête = 1 crédit (minimum)
        assert res['billing']['billing_mode'] == 'managed'
        assert res['billing']['credits_charged'] == 1
        assert res['billing']['credits_remaining'] == 2
        assert store.balance(kh) == 2

    def test_document_rejete_ne_consomme_rien(self, billing_env):
        client, key, _, store = billing_env
        kh = hash_key(key)
        r = _analyze(client, key, data=b'%PDF-fake', filename='x.pdf')
        assert r.status_code == 202  # erreur portée par le job
        _result(client, key, r.json()['job_id'])
        assert store.balance(kh) == 3  # intact

    def test_credits_epuises_402(self, billing_env):
        client, key, _, store = billing_env
        store.deduct(hash_key(key), 3)  # solde à 0
        r = _analyze(client, key)
        assert r.status_code == 402
        assert r.json()['error']['code'] == 'payment_required'

    def test_cle_non_geree_non_facturee(self, billing_env, monkeypatch):
        client, _, _, _ = billing_env
        monkeypatch.setattr(api_mod, 'api_keys', ApiKeyStore([]))
        r = _analyze(client, 'cle-libre-non-geree')
        assert r.status_code == 202
        res = _result(client, 'cle-libre-non-geree',
                      r.json()['job_id']).json()
        assert res['billing']['billing_mode'] == 'unmanaged'

    def test_usage_affiche_le_solde(self, billing_env):
        client, key, _, _ = billing_env
        r = client.get('/v1/usage', headers={'X-API-Key': key})
        assert r.json()['credits_remaining'] == 3
        assert r.json()['billing_mode'] == 'managed'


class TestTopup:
    def test_catalogue_packs_public(self, billing_env):
        client, _, _, _ = billing_env
        r = client.get('/v1/billing/packs')
        assert r.status_code == 200
        assert {p['pack'] for p in r.json()['packs']} == \
            {'starter', 'pro', 'business'}

    def test_pack_inconnu_400(self, billing_env):
        client, key, _, _ = billing_env
        r = client.post('/v1/billing/topup',
                        json={'pack': 'mega', 'customer_email': 'a@b.sn',
                              'customer_name': 'A'},
                        headers={'X-API-Key': key})
        assert r.status_code == 400

    def test_email_invalide_400(self, billing_env):
        client, key, _, _ = billing_env
        r = client.post('/v1/billing/topup',
                        json={'pack': 'pro', 'customer_email': 'pas-un-email',
                              'customer_name': 'A'},
                        headers={'X-API-Key': key})
        assert r.status_code == 400

    def test_topup_cree_session_pending(self, billing_env):
        client, key, gateway, _ = billing_env
        r = client.post('/v1/billing/topup',
                        json={'pack': 'starter', 'customer_email': 'a@b.sn',
                              'customer_name': 'Awa'},
                        headers={'X-API-Key': key})
        assert r.status_code == 200
        body = r.json()
        assert body['status'] == 'pending'
        assert body['credits'] == 500 and body['amount_fcfa'] == 5000
        assert body['checkout_url'].startswith('https://pay.saspay.test/')


class TestWebhookEtActivation:
    def _buy(self, client, key, pack='starter'):
        r = client.post('/v1/billing/topup',
                        json={'pack': pack, 'customer_email': 'a@b.sn',
                              'customer_name': 'Awa'},
                        headers={'X-API-Key': key})
        return r.json()['reference']

    def _webhook_post(self, client, gateway, session_id, success=True):
        webhook = gateway.pay_session(session_id, success=success)
        body, ts, sig, event = webhook
        return client.post('/v1/billing/webhook', content=body, headers={
            'Content-Type': 'application/json',
            'X-Webhook-Signature': sig,
            'X-Webhook-Timestamp': ts,
            'X-Webhook-Event': event})

    def test_webhook_sans_signature_401(self, billing_env):
        client, key, gateway, _ = billing_env
        ref = self._buy(client, key)
        assert ref
        r = client.post('/v1/billing/webhook',
                        content=b'{"event":"transaction.success"}',
                        headers={'Content-Type': 'application/json'})
        assert r.status_code == 401
        assert r.json()['error']['code'] == 'invalid_signature'

    def test_webhook_signature_invalide_401(self, billing_env):
        client, key, gateway, _ = billing_env
        ref = self._buy(client, key)
        session = gateway._sessions  # retrouver la session
        sid = list(session.keys())[0]
        gateway.pay_session(sid, success=True)  # consomme l'état PENDING
        body, ts, sig, ev = gateway.build_webhook('transaction.success',
                                                  {'id': 'tx-x'})
        r = client.post('/v1/billing/webhook', content=body, headers={
            'X-Webhook-Signature': 'f' * 64,
            'X-Webhook-Timestamp': ts,
            'X-Webhook-Event': ev})
        assert r.status_code == 401

    def test_achat_complet_webhook_active_credits(self, billing_env):
        client, key, gateway, store = billing_env
        kh = hash_key(key)
        ref = self._buy(client, key)
        sid = api_mod.billing_store.get(ref)['session_id']
        # le client paie (simulé) → webhook transaction.success
        r = self._webhook_post(client, gateway, sid, success=True)
        assert r.status_code == 200, r.text
        assert r.json().get('activated') is True
        # crédits activés : 3 initiaux + 500 du pack
        assert store.balance(kh) == 503
        # statut du rechargement : activated
        r2 = client.get(f'/v1/billing/status/{ref}',
                        headers={'X-API-Key': key})
        assert r2.json()['status'] == 'activated'
        # l'API répond maintenant aux traitements
        assert _analyze(client, key).status_code == 202

    def test_webhook_double_ne_recredite_pas(self, billing_env):
        client, key, gateway, store = billing_env
        kh = hash_key(key)
        ref = self._buy(client, key)
        sid = api_mod.billing_store.get(ref)['session_id']
        body, ts, sig, ev = gateway.pay_session(sid, success=True)
        # renvoyer le MÊME événement tel quel (rejeu réseau)
        headers = {'X-Webhook-Signature': sig, 'X-Webhook-Timestamp': ts,
                   'X-Webhook-Event': ev,
                   'Content-Type': 'application/json'}
        r1 = client.post('/v1/billing/webhook', content=body, headers=headers)
        r2 = client.post('/v1/billing/webhook', content=body, headers=headers)
        r3 = client.post('/v1/billing/webhook', content=body, headers=headers)
        assert r1.json().get('activated') is True
        assert r2.json().get('duplicate') is True
        assert r3.json().get('duplicate') is True
        assert store.balance(kh) == 503  # crédité UNE seule fois

    def test_paiement_echoue_marque_failed(self, billing_env):
        client, key, gateway, store = billing_env
        kh = hash_key(key)
        ref = self._buy(client, key)
        sid = api_mod.billing_store.get(ref)['session_id']
        r = self._webhook_post(client, gateway, sid, success=False)
        assert r.status_code == 200
        assert store.balance(kh) == 3  # aucun crédit ajouté
        st = client.get(f'/v1/billing/status/{ref}',
                        headers={'X-API-Key': key}).json()
        assert st['status'] == 'failed'

    def test_polling_rattrape_webhook_manque(self, billing_env):
        """Sans webhook reçu, GET /billing/status/{ref} interroge l'état
        réel et active les crédits (fallback documenté SasPay)."""
        client, key, gateway, store = billing_env
        kh = hash_key(key)
        ref = self._buy(client, key)
        sid = api_mod.billing_store.get(ref)['session_id']
        gateway.pay_session(sid, success=True)  # payé, webhook "perdu"
        r = client.get(f'/v1/billing/status/{ref}',
                       headers={'X-API-Key': key})
        assert r.json()['status'] == 'activated'
        assert store.balance(kh) == 503

    def test_statut_isole_par_cle(self, billing_env, monkeypatch):
        client, key, gateway, _ = billing_env
        monkeypatch.setattr(api_mod, 'api_keys', ApiKeyStore([]))
        ref = self._buy(client, key)
        # une AUTRE clé (valide) ne voit pas ce rechargement
        r = client.get(f'/v1/billing/status/{ref}',
                       headers={'X-API-Key': 'autre-cle-valide'})
        assert r.status_code == 404

    def test_webhook_desactive_sans_secret(self, billing_env, monkeypatch):
        client, key, gateway, _ = billing_env
        monkeypatch.delenv('STATEMENTFLOW_SASPAY_WEBHOOK_SECRET',
                           raising=False)
        ref = self._buy(client, key)
        sid = api_mod.billing_store.get(ref)['session_id']
        r = self._webhook_post(client, gateway, sid, success=True)
        assert r.status_code == 503  # webhook désactivé sans secret


class TestCleRevoquee:
    def test_revocation_bloque_tout(self, billing_env):
        client, key, gateway, store = billing_env
        assert _analyze(client, key).status_code == 202
        store.revoke(hash_key(key))
        r = _analyze(client, key)
        assert r.status_code == 401  # clé gérée révoquée → refusée


class TestRouteSimulateurVerrouillee:
    """La route de simulation de paiement doit être INEXISTANTE sauf
    double condition explicite (mode test + drapeau)."""

    def test_404_sans_drapeau(self, billing_env):
        client, key, gateway, _ = billing_env
        r = client.post('/v1/billing/_test/pay', json={'session_id': 'x'})
        assert r.status_code == 404

    def test_404_avec_drapeau_mais_route_interrogee_anonymement(self, billing_env,
                                                                monkeypatch):
        client, key, gateway, _ = billing_env
        monkeypatch.setenv('STATEMENTFLOW_TEST_PAYMENT', '1')
        r = client.post('/v1/billing/_test/pay', json={'session_id': 'x'})
        assert r.status_code == 404  # session inconnue → 404, pas 200

    def test_active_uniquement_en_mode_simulateur(self, billing_env, monkeypatch):
        """Avec un provider réel (SasPayClient), la route est 404 même
        avec le drapeau : aucune simulation possible en production."""
        from statementflow.billing import SasPayClient
        client, key, gateway, _ = billing_env
        monkeypatch.setenv('STATEMENTFLOW_TEST_PAYMENT', '1')
        monkeypatch.setattr(api_mod, 'saspay',
                            SasPayClient('sk_live-fake'))
        monkeypatch.setattr(api_mod, 'billing_mode', 'saspay')
        r = client.post('/v1/billing/_test/pay',
                        json={'session_id': 'nimporte'})
        assert r.status_code == 404
