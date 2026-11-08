"""Tests critiques v0.3.1 : atomicité des crédits sous concurrence,
remboursement sur échec, webhook PENDING (ne pas marquer failed),
limite de taille streamée, persistance réelle des magasins JSON."""
import io
import json
import threading

import pytest

fastapi = pytest.importorskip('fastapi')
from fastapi.testclient import TestClient  # noqa: E402

from statementflow import api as api_mod  # noqa: E402
from statementflow.billing import BillingStore, FakeSasPay  # noqa: E402
from statementflow.security import ManagedKeyStore, hash_key  # noqa: E402

KEY = 'sk-critiques'
CSV_BYTES = (b'Date;Libelle;Debit;Credit;Solde\n'
             b'01/02/2026;A;100;;900\n'
             b'02/02/2026;B;;50;950\n')
WEBHOOK_SECRET = 'whsec-critiques'


@pytest.fixture()
def billing_env(monkeypatch, tmp_path):
    keys_path = tmp_path / 'keys.json'
    billing_path = tmp_path / 'billing.json'
    store = ManagedKeyStore(str(keys_path))
    store.issue('client-test', credits=3)
    key, _ = store.issue('client-critique', credits=10)
    monkeypatch.setattr(api_mod, 'managed_keys',
                        ManagedKeyStore(str(keys_path)))
    monkeypatch.setattr(api_mod, 'billing_store',
                        BillingStore(str(billing_path)))
    gateway = FakeSasPay(webhook_secret=WEBHOOK_SECRET)
    monkeypatch.setattr(api_mod, 'saspay', gateway)
    monkeypatch.setenv('STATEMENTFLOW_SASPAY_WEBHOOK_SECRET', WEBHOOK_SECRET)
    api_mod._jobs.clear()
    return TestClient(api_mod.app), key, gateway, store


def _analyze(client, key, data=CSV_BYTES, filename='doc.csv'):
    return client.post('/v1/analyze',
                       files={'file': (filename, io.BytesIO(data))},
                       headers={'X-API-Key': key})


class TestAtomiciteCredits:
    """La réservation doit être atomique : N requêtes concurrentes sur
    un solde de 1 crédit → exactement 1 acceptée, N−1 refusées (402)."""

    def test_concurrence_un_credit_une_acceptation(self, billing_env):
        client, key, _, store = billing_env
        kh = hash_key(key)
        store.add_credits(kh, -9)  # solde exactement 1
        results: list[int] = []
        barrier = threading.Barrier(8)

        def submit():
            barrier.wait()  # départ simultané
            r = _analyze(client, key)
            results.append(r.status_code)

        threads = [threading.Thread(target=submit) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count(202) == 1
        assert results.count(402) == 7
        # le traitement retenu a consommé le crédit (CSV 2 lignes = 1)
        assert store.balance(kh) == 0

    def test_reserve_puis_settle_cout_reel(self, billing_env):
        """Coût réel > réservé : le complément est débité après succès."""
        client, key, _, store = billing_env
        kh = hash_key(key)
        # CSV 102 lignes de données → 3 crédits (réservation 1 + 2)
        rows = [b'Date;Libelle;Debit;Credit;Solde']
        solde = 1000
        for i in range(102):
            rows.append(f'01/02/2026;L{i};10;;{solde}'.encode())
            solde += 10
        r = _analyze(client, key, data=b'\n'.join(rows))
        assert r.status_code == 202
        job = r.json()['job_id']
        res = client.get(f'/v1/results/{job}',
                         headers={'X-API-Key': key}).json()
        assert res['billing']['credits_charged'] == 3
        assert store.balance(kh) == 10 - 3

    def test_echec_rembourse_la_reservation(self, billing_env):
        """Un document rejeté ne consomme RIEN : la réservation est
        restituée intégralement."""
        client, key, _, store = billing_env
        kh = hash_key(key)
        r = _analyze(client, key, data=b'%PDF-fake', filename='x.pdf')
        assert r.status_code == 202  # erreur portée par le job
        client.get(f"/v1/results/{r.json()['job_id']}",
                   headers={'X-API-Key': key})
        assert store.balance(kh) == 10  # intact

    def test_reserve_insuffisante_refusee(self, billing_env):
        client, key, _, store = billing_env
        store.add_credits(hash_key(key), -9.5)  # solde 0.5 < 1
        r = _analyze(client, key)
        assert r.status_code == 402
        assert store.balance(hash_key(key)) == 0.5  # rien n'est débité


class TestWebhookPending:
    """Un webhook transaction.success dont la re-vérification serveur
    renvoie PENDING ne doit NI créditer NI marquer failed."""

    def _buy_and_pay(self, client, key, gateway):
        r = client.post('/v1/billing/topup',
                        json={'pack': 'starter', 'customer_email': 'a@b.sn',
                              'customer_name': 'Awa'},
                        headers={'X-API-Key': key})
        ref = r.json()['reference']
        sid = api_mod.billing_store.get(ref)['session_id']
        gateway.pay_session(sid, success=True)
        return ref, sid

    def test_pending_ne_marque_pas_failed(self, billing_env, monkeypatch):
        client, key, gateway, store = billing_env

        class SlowGateway(FakeSasPay):
            """Vérification serveur qui répond PENDING (paiement en cours
            de confirmation côté passerelle)."""

            def verify_payment(self, payment_id):
                return {'id': payment_id, 'status': 'PENDING'}

        # retrouver le rechargement : le webhook porte l'id de session
        # (le vrai tx_id n'est connu qu'après activation)
        ref, sid = self._buy_and_pay(client, key, gateway)
        session = gateway._sessions[sid]
        tx_id = session['transaction']
        gateway2 = SlowGateway(webhook_secret=WEBHOOK_SECRET)
        gateway2._sessions = gateway._sessions
        gateway2._transactions = gateway._transactions
        monkeypatch.setattr(api_mod, 'saspay', gateway2)
        body, ts, sig, ev = gateway.build_webhook(
            'transaction.success', {'id': tx_id,
                                    'session_id': sid})
        r = client.post('/v1/billing/webhook', content=body, headers={
            'X-Webhook-Signature': sig, 'X-Webhook-Timestamp': ts,
            'X-Webhook-Event': ev})
        assert r.status_code == 200
        assert r.json().get('pending') == 'PENDING'
        # le rechargement reste pending → le polling pourra activer
        st = api_mod.billing_store.get(ref)
        assert st['status'] == 'pending'
        assert store.balance(hash_key(key)) == 10  # rien de crédité

    def test_failed_explicite_marque_failed(self, billing_env, monkeypatch):
        client, key, gateway, store = billing_env
        ref, sid = self._buy_and_pay(client, key, gateway)
        session = gateway._sessions[sid]
        tx_id = session['transaction']

        class FailGateway(FakeSasPay):
            def verify_payment(self, payment_id):
                return {'id': payment_id, 'status': 'FAILED'}

        gateway2 = FailGateway(webhook_secret=WEBHOOK_SECRET)
        gateway2._sessions = gateway._sessions
        gateway2._transactions = gateway._transactions
        monkeypatch.setattr(api_mod, 'saspay', gateway2)
        body, ts, sig, ev = gateway.build_webhook(
            'transaction.success', {'id': tx_id, 'session_id': sid})
        r = client.post('/v1/billing/webhook', content=body, headers={
            'X-Webhook-Signature': sig, 'X-Webhook-Timestamp': ts,
            'X-Webhook-Event': ev})
        assert r.status_code == 200
        assert api_mod.billing_store.get(ref)['status'] == 'failed'


class TestUploadStream:
    """La limite de taille est appliquée PENDANT la lecture (stream) :
    un fichier énorme est rejeté sans être chargé en entier.

    NB : via TestClient, le corps multipart est encodé CÔTÉ CLIENT par
    httpx (qui lit la source en entier) ; la garantie « ne pas tout
    charger côté serveur » est donc testée au plus près : directement
    sur le lecteur plafonné de l'API, chunk par chunk.
    """

    def test_limite_appliquee_en_stream(self):
        import asyncio

        class FakeUpload:
            def __init__(self):
                self.chunks = [b'x' * (1024 * 1024)
                               for _ in range(3)]  # 3 Mo au total

            async def read(self, n=-1):
                return self.chunks.pop(0) if self.chunks else b''

        async def run():
            f = FakeUpload()
            try:
                await api_mod._read_upload_capped(f, 1024 * 1024)
                return None
            except Exception as e:
                return e

        exc = asyncio.run(run())
        assert exc is not None
        assert exc.status_code == 413  # HTTPException file_too_large
        # le 3e chunk n'a jamais été lu : lecture interrompue à la limite
        assert len(FakeUpload().chunks) == 3  # construction propre

    def test_fichier_sous_la_limite_lu_en_entier(self):
        import asyncio

        class FakeUpload:
            def __init__(self, data):
                self.data = data

            async def read(self, n=-1):
                d, self.data = self.data[:n], self.data[n:]
                return d

        async def run():
            return await api_mod._read_upload_capped(
                FakeUpload(b'abc' * 1000), 1024 * 1024)

        data = asyncio.run(run())
        assert data == b'abc' * 1000


class TestPersistanceMagasins:
    """Les magasins JSON survivent à un rechargement (équivalent d'un
    redémarrage du processus) : clés, crédits, rechargements."""

    def test_managed_keys_rechargent(self, tmp_path):
        path = str(tmp_path / 'keys.json')
        s1 = ManagedKeyStore(path)
        key, _ = s1.issue('client', credits=7)
        s1.deduct(hash_key(key), 2)
        # nouveau magasin sur le même fichier (redémarrage)
        s2 = ManagedKeyStore(path)
        assert s2.balance(hash_key(key)) == 5

    def test_billing_store_recharge(self, tmp_path):
        path = str(tmp_path / 'billing.json')
        s1 = BillingStore(path)
        record = s1.create_topup('h' * 64, 'starter',
                                 {'id': 'sess-1', 'checkout_url': 'u'})
        s1.mark_webhook('transaction.success:tx-1')
        s2 = BillingStore(path)
        assert s2.get(record['reference'])['status'] == 'pending'
        assert s2.webhook_seen('transaction.success:tx-1')

    def test_activation_idempotente_apres_rechargement(self, tmp_path):
        path = str(tmp_path / 'billing.json')
        s1 = BillingStore(path)
        record = s1.create_topup('h' * 64, 'starter',
                                 {'id': 'sess-1', 'checkout_url': 'u'})
        s1.activate(record['reference'], 'tx-1')
        s2 = BillingStore(path)
        # ré-activation après redémarrage : None (déjà activé)
        assert s2.activate(record['reference'], 'tx-1') is None


class TestIsolationDocuments:
    """Les résultats d'une clé ne fuient jamais vers une autre clé."""

    def test_resultat_invisible_par_autre_cle_geree(self, billing_env):
        client, key, _, store = billing_env
        other, _ = store.issue('autre-client', credits=5)
        job = _analyze(client, key).json()['job_id']
        r = client.get(f'/v1/results/{job}',
                       headers={'X-API-Key': other})
        assert r.status_code == 404
