#!/usr/bin/env python3
"""Test de l'API REST par TestClient FastAPI (sans réseau).

v0.2.0 : authentification X-API-Key requise + administration séparée.
"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# la clé d'administration doit être configurée AVANT l'import de l'API
os.environ.setdefault('STATEMENTFLOW_ADMIN_KEY', 'admin-secret-manual')
import sys  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, 'src'))

from fastapi.testclient import TestClient  # noqa: E402

from statementflow.api import app  # noqa: E402

client = TestClient(app)
CORPUS = os.path.join(ROOT, 'tests', 'data', 'corpus')
KEY = 'manual-check-key'
ADMIN = {'X-Admin-Key': os.environ['STATEMENTFLOW_ADMIN_KEY']}


def post(path):
    with open(path, 'rb') as f:
        return client.post(
            '/v1/analyze',
            files={'file': (path.split('/')[-1], f)},
            headers={'X-API-Key': KEY})


# 1. health (public, sans clé)
r = client.get('/health')
assert r.status_code == 200 and r.json()['status'] == 'ok', r.text
print('health →', r.json()['status'], '| mode auth :', r.json()['auth_mode'])

# 2. OpenAPI documentée
r = client.get('/openapi.json')
paths = r.json()['paths']
assert '/v1/analyze' in paths and '/v1/results/{job_id}' in paths
assert '/v1/admin/usage' in paths
print('openapi →', sorted(paths))

# 3. sans clé → 401 structuré
r = client.post('/v1/analyze', files={'file': ('x.csv', io.BytesIO(b'a,b\n1,2\n'))})
assert r.status_code == 401 and r.json()['error']['code'] == 'unauthorized', r.text
print('sans clé → 401', r.json()['error']['code'], '| X-Request-ID :',
      r.headers['X-Request-ID'][:8] + '…')

# 4. analyze PDF → done immédiat (traitement synchrone rapide)
r = post(f'{CORPUS}/SYNTH_fr_societegenerale_A.pdf')
assert r.status_code == 202, r.text
job = r.json()
print('analyze →', job['job_id'][:12], job['status'])

# 5. results (clé propriétaire)
r = client.get(f"/v1/results/{job['job_id']}", headers={'X-API-Key': KEY})
assert r.status_code == 200
data = r.json()
assert data['status'] == 'done' and len(data['transactions']) == 14
assert data['validation']['status'] in ('validated', 'warning',
                                        'inconsistent', 'unverifiable')
print('results → 14 tx, validation', data['validation']['status'],
      '| chaîne', data['checks']['chain_matches'], '/',
      data['checks']['chain_checked'])

# 6. usage de la clé (masquée, jamais en clair)
r = client.get('/v1/usage', headers={'X-API-Key': KEY})
assert r.json()['documents'] == 1
assert KEY not in r.text
print('usage →', r.json()['key_masked'], '| documents :',
      r.json()['documents'])

# 7. erreurs : fichier vide
r = client.post('/v1/analyze',
                files={'file': ('vide.csv', io.BytesIO(b''))},
                headers={'X-API-Key': KEY})
assert r.status_code == 400, r.text
print('fichier vide → 400', r.json()['error']['code'])

# 8. erreurs : PDF corrompu (erreur portée par le job, modèle asynchrone)
r = client.post('/v1/analyze',
                files={'file': ('fake.pdf', io.BytesIO(b'%PDF-pas-un-vrai-pdf'))},
                headers={'X-API-Key': KEY})
assert r.status_code == 202, r.text
r = client.get(f"/v1/results/{r.json()['job_id']}", headers={'X-API-Key': KEY})
assert r.status_code == 422 and r.json()['status'] == 'error', r.text
print('PDF corrompu → 422', r.json()['error'][:70])

# 9. résultat inconnu
r = client.get('/v1/results/nimporte', headers={'X-API-Key': KEY})
assert r.status_code == 404
print('résultat inconnu → 404 OK')

# 10. réinitialisation : refusée avec la clé API, acceptée avec l'admin
from statementflow.security import hash_key
h = hash_key(KEY)
r = client.post('/v1/admin/usage/reset', json={'key_hash': h},
                headers={'X-API-Key': KEY})
assert r.status_code == 403, r.text
r = client.post('/v1/admin/usage/reset', json={'key_hash': h}, headers=ADMIN)
assert r.status_code == 200 and r.json()['reset'] is True, r.text
after = client.get('/v1/usage', headers={'X-API-Key': KEY}).json()
assert after['documents'] == 0
print('admin reset → clé API refusée (403), admin OK, compteurs à 0')

print('\nAPI TestClient : TOUS LES TESTS PASSENT')
