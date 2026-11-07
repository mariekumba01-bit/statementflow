#!/usr/bin/env python3
"""Test de l'API REST par TestClient FastAPI (sans réseau)."""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from fastapi.testclient import TestClient

from statementflow.api import app

client = TestClient(app)
CORPUS = os.path.join(ROOT, 'tests', 'data', 'corpus')


def post(path):
    with open(path, 'rb') as f:
        return client.post(
            '/v1/analyze',
            files={'file': (path.split('/')[-1], f)},
            headers={'X-API-Key': 'test-key'})


# 1. health
r = client.get('/health')
assert r.status_code == 200 and r.json()['status'] == 'ok', r.text
print('health →', r.json())

# 2. OpenAPI documentée
r = client.get('/openapi.json')
paths = r.json()['paths']
assert '/v1/analyze' in paths and '/v1/results/{job_id}' in paths
print('openapi →', sorted(paths))

# 3. analyze PDF → done immédiat (traitement synchrone rapide)
r = post(f'{CORPUS}/SYNTH_fr_societegenerale_A.pdf')
assert r.status_code == 202, r.text
job = r.json()
print('analyze →', job['job_id'][:12], job['status'])

# 4. results
r = client.get(f"/v1/results/{job['job_id']}")
assert r.status_code == 200
data = r.json()
assert data['status'] == 'done' and len(data['transactions']) == 14
print('results → 14 tx, chaîne', data['checks']['chain_matches'], '/',
      data['checks']['chain_checked'])

# 5. usage de la clé
r = client.get('/v1/usage', headers={'X-API-Key': 'test-key'})
print('usage →', r.json())
assert r.json()['documents'] == 1

# 6. erreurs : fichier vide
r = client.post('/v1/analyze',
                files={'file': ('vide.csv', io.BytesIO(b''))})
assert r.status_code == 400, r.text
print('fichier vide → 400', r.json()['detail'])

# 7. erreurs : PDF corrompu (erreur portée par le job, modèle asynchrone)
r = client.post('/v1/analyze',
                files={'file': ('fake.pdf', io.BytesIO(b'%PDF-pas-un-vrai-pdf'))})
assert r.status_code == 202, r.text
r = client.get(f"/v1/results/{r.json()['job_id']}")
assert r.status_code == 422 and r.json()['status'] == 'error', r.text
print('PDF corrompu → 422', r.json()['error'][:70])

# 8. extension mensongère (erreur immédiate, portée par le job)
r = client.post('/v1/analyze',
                files={'file': ('photo.jpg', io.BytesIO(b'\x89PNG\r\n\x1a\n'))})
assert r.status_code == 202, r.text
r = client.get(f"/v1/results/{r.json()['job_id']}")
assert r.status_code == 422, r.text
print('extension mensongère → 422', r.json()['error'][:60])

# 9. résultat inconnu
r = client.get('/v1/results/nimporte')
assert r.status_code == 404
print('résultat inconnu → 404 OK')

# 10. reset usage
r = client.delete('/v1/usage/test-key')
assert client.get('/v1/usage', headers={'X-API-Key': 'test-key'}).json()['documents'] == 0
print('reset usage → OK')

print('\nAPI TestClient : TOUS LES TESTS PASSENT')
