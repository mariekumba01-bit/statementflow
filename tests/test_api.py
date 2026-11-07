"""Tests pytest de l'API REST (TestClient, sans réseau)."""
import io
import os

import pytest

fastapi = pytest.importorskip('fastapi')
from fastapi.testclient import TestClient  # noqa: E402

from statementflow.api import app  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, 'data', 'corpus')


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
        assert 'max_file_mb' in body['limits']

    def test_openapi_documentee(self, client):
        r = client.get('/openapi.json')
        assert r.status_code == 200
        paths = r.json()['paths']
        for p in ('/health', '/v1/analyze', '/v1/results/{job_id}', '/v1/usage'):
            assert p in paths


class TestAnalyze:
    def test_pdf_vers_json(self, client):
        r = post_doc(client, f'{CORPUS}/SYNTH_fr_societegenerale_A.pdf')
        assert r.status_code == 202
        job = r.json()
        assert job['status'] == 'done'
        r2 = client.get(f"/v1/results/{job['job_id']}")
        assert r2.status_code == 200
        data = r2.json()
        assert data['status'] == 'done'
        assert len(data['transactions']) == 14
        assert data['checks']['opening_closing_ok'] is True

    def test_csv_avec_currency_hint(self, client):
        with open(f'{CORPUS}/SYNTH_csv_orange_money_V.csv', 'rb') as f:
            r = client.post('/v1/analyze',
                            files={'file': ('om.csv', f)},
                            headers={'X-Currency-Hint': 'XOF'})
        data = client.get(f"/v1/results/{r.json()['job_id']}").json()
        assert data['currency'] == 'XOF'

    def test_usage_compte(self, client):
        key = 'usage-test'
        client.delete(f'/v1/usage/{key}')
        post_doc(client, f'{CORPUS}/SYNTH_csv_mtn_momo_W.csv', key)
        r = client.get('/v1/usage', headers={'X-API-Key': key})
        assert r.json()['documents'] == 1
        assert r.json()['transactions'] == 10


class TestErreurs:
    def test_fichier_vide(self, client):
        r = client.post('/v1/analyze',
                        files={'file': ('vide.csv', io.BytesIO(b''))})
        assert r.status_code == 400

    def test_pdf_corrompu(self, client):
        r = client.post('/v1/analyze',
                        files={'file': ('fake.pdf',
                                        io.BytesIO(b'%PDF-pas-un-pdf'))})
        assert r.status_code == 202  # erreur portée par le job
        r2 = client.get(f"/v1/results/{r.json()['job_id']}")
        assert r2.status_code == 422
        assert 'corrompu' in r2.json()['error']

    def test_extension_mensongere(self, client):
        r = client.post('/v1/analyze',
                        files={'file': ('photo.jpg',
                                        io.BytesIO(b'\x89PNG\r\n\x1a\n'))})
        r2 = client.get(f"/v1/results/{r.json()['job_id']}")
        assert r2.status_code == 422

    def test_fichier_trop_gros(self, client, monkeypatch):
        from statementflow import api as api_mod
        monkeypatch.setattr(api_mod.config, 'max_file_mb', 0.001)
        r = client.post('/v1/analyze',
                        files={'file': ('gros.csv', io.BytesIO(b'x' * 2048))})
        assert r.status_code == 413

    def test_resultat_inconnu(self, client):
        assert client.get('/v1/results/inconnu').status_code == 404

    def test_quota(self, client, monkeypatch):
        from statementflow import api as api_mod
        monkeypatch.setitem(api_mod.quotas, 'quota-key',
                            api_mod.Quota(max_documents=1))
        client.delete('/v1/usage/quota-key')
        assert post_doc(client, f'{CORPUS}/SYNTH_csv_moov_X.csv',
                        'quota-key').status_code == 202
        r = post_doc(client, f'{CORPUS}/SYNTH_csv_moov_X.csv', 'quota-key')
        assert r.status_code == 429
