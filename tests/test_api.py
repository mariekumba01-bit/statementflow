"""Tests pytest de l'API REST (TestClient, sans réseau).

v0.2.0 : toutes les routes privées exigent X-API-Key (voir test_security.py
pour la matrice d'authentification complète) ; ces tests couvrent le
fonctionnement nominal et les erreurs de traitement.
"""
import io
import os
import uuid

import pytest

fastapi = pytest.importorskip('fastapi')
from fastapi.testclient import TestClient  # noqa: E402

from statementflow.api import app  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, 'data', 'corpus')


@pytest.fixture(autouse=True)
def _explicit_open_auth(monkeypatch):
    """Ces tests couvrent le TRAITEMENT nominal, pas l'authentification
    (matrice complète dans test_security.py) : le mode ouvert y est
    activé EXPLICITEMENT, comme en développement local
    (STATEMENTFLOW_ALLOW_OPEN_AUTH=1). Sans ce drapeau — et sans clé
    configurée — l'API refuse toute requête privée (503)."""
    monkeypatch.setenv('STATEMENTFLOW_ALLOW_OPEN_AUTH', '1')


@pytest.fixture()
def client():
    return TestClient(app)


def post_doc(client, path, key='pytest'):
    with open(path, 'rb') as f:
        return client.post('/v1/analyze',
                           files={'file': (os.path.basename(path), f)},
                           headers={'X-API-Key': key})


class TestHealth:
    def test_health(self, client):
        r = client.get('/health')
        assert r.status_code == 200
        body = r.json()
        assert body['status'] == 'ok'
        assert body['version'] == __import__('statementflow').__version__
        assert 'max_file_mb' in body['limits']
        assert body['auth_mode'] in ('open', 'key-hash-list',
                                     'key-hash-list+managed-keys',
                                     'managed-keys', 'locked')

    def test_openapi_documentee(self, client):
        r = client.get('/openapi.json')
        assert r.status_code == 200
        paths = r.json()['paths']
        for p in ('/health', '/v1/analyze', '/v1/results/{job_id}',
                  '/v1/usage', '/v1/admin/usage', '/v1/admin/usage/reset'):
            assert p in paths


class TestAnalyze:
    def test_pdf_vers_json(self, client):
        r = post_doc(client, f'{CORPUS}/SYNTH_fr_societegenerale_A.pdf')
        assert r.status_code == 202
        job = r.json()
        assert job['status'] == 'done'
        r2 = client.get(f"/v1/results/{job['job_id']}",
                        headers={'X-API-Key': 'pytest'})
        assert r2.status_code == 200
        data = r2.json()
        assert data['status'] == 'done'
        assert len(data['transactions']) == 14
        assert data['checks']['opening_closing_ok'] is True

    def test_csv_avec_currency_hint(self, client):
        with open(f'{CORPUS}/SYNTH_csv_orange_money_V.csv', 'rb') as f:
            r = client.post('/v1/analyze',
                            files={'file': ('om.csv', f)},
                            headers={'X-API-Key': 'pytest',
                                     'X-Currency-Hint': 'XOF'})
        data = client.get(f"/v1/results/{r.json()['job_id']}",
                          headers={'X-API-Key': 'pytest'}).json()
        assert data['currency'] == 'XOF'

    def test_usage_compte(self, client):
        from statementflow.api import usage_store
        from statementflow.security import hash_key
        key = f'usage-test-{uuid.uuid4().hex[:8]}'
        post_doc(client, f'{CORPUS}/SYNTH_csv_mtn_momo_W.csv', key)
        r = client.get('/v1/usage', headers={'X-API-Key': key})
        assert r.json()['documents'] == 1
        assert r.json()['transactions'] == 10
        # la clé en clair ne doit JAMAIS revenir dans la réponse
        assert key not in r.text
        assert r.json()['key_hash'] == hash_key(key)
        usage_store.reset(hash_key(key))


class TestErreurs:
    def test_fichier_vide(self, client):
        r = client.post('/v1/analyze',
                        files={'file': ('vide.csv', io.BytesIO(b''))},
                        headers={'X-API-Key': 'pytest'})
        assert r.status_code == 400
        assert r.json()['error']['code'] == 'invalid_file'

    def test_pdf_corrompu(self, client):
        r = client.post('/v1/analyze',
                        files={'file': ('fake.pdf',
                                        io.BytesIO(b'%PDF-pas-un-pdf'))},
                        headers={'X-API-Key': 'pytest'})
        assert r.status_code == 202  # erreur portée par le job
        r2 = client.get(f"/v1/results/{r.json()['job_id']}",
                        headers={'X-API-Key': 'pytest'})
        assert r2.status_code == 422
        assert 'corrompu' in r2.json()['error']

    def test_extension_mensongere(self, client):
        r = client.post('/v1/analyze',
                        files={'file': ('photo.jpg',
                                        io.BytesIO(b'\x89PNG\r\n\x1a\n'))},
                        headers={'X-API-Key': 'pytest'})
        r2 = client.get(f"/v1/results/{r.json()['job_id']}",
                        headers={'X-API-Key': 'pytest'})
        assert r2.status_code == 422

    def test_fichier_trop_gros(self, client, monkeypatch):
        from statementflow import api as api_mod
        monkeypatch.setattr(api_mod.config, 'max_file_mb', 0.001)
        r = client.post('/v1/analyze',
                        files={'file': ('gros.csv', io.BytesIO(b'x' * 2048))},
                        headers={'X-API-Key': 'pytest'})
        assert r.status_code == 413
        assert r.json()['error']['code'] == 'file_too_large'

    def test_resultat_inconnu(self, client):
        r = client.get('/v1/results/inconnu', headers={'X-API-Key': 'pytest'})
        assert r.status_code == 404
        assert r.json()['error']['code'] == 'not_found'

    def test_quota(self, client, monkeypatch):
        from statementflow import api as api_mod
        from statementflow.security import hash_key
        monkeypatch.setattr(api_mod.quota, 'max_documents', 1)
        key = f'quota-key-{uuid.uuid4().hex[:8]}'
        api_mod.usage_store.reset(hash_key(key))
        assert post_doc(client, f'{CORPUS}/SYNTH_csv_moov_X.csv',
                        key).status_code == 202
        r = post_doc(client, f'{CORPUS}/SYNTH_csv_moov_X.csv', key)
        assert r.status_code == 429
        assert r.json()['error']['code'] == 'quota_exceeded'
        api_mod.usage_store.reset(hash_key(key))
