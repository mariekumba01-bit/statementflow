"""Smoke test hors Docker du comportement de docker/start.sh et de
l'API verrouillée : simule le lancement Render (PORT injecté, aucune
clé configurée) et vérifie /health + refus 503 des routes privées."""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

PORT = '8199'
env = dict(os.environ)
env.update({
    'PORT': PORT,
    'STATEMENTFLOW_API_HOST': '127.0.0.1',
    # AUCUNE clé configurée, AUCUN drapeau d'ouverture → verrou attendu
    'STATEMENTFLOW_API_KEY_HASHES': '',
    'STATEMENTFLOW_KEYS_PATH': '',
    'STATEMENTFLOW_ALLOW_OPEN_AUTH': '',
})
for var in ('STATEMENTFLOW_API_KEY_HASHES', 'STATEMENTFLOW_KEYS_PATH',
            'STATEMENTFLOW_ALLOW_OPEN_AUTH'):
    env.pop(var, None)

# lancer exactement comme start.sh (uvicorn, PORT dynamique)
proc = subprocess.Popen(
    [sys.executable, '-m', 'uvicorn', 'statementflow.api:app',
     '--host', '127.0.0.1', '--port', PORT,
     '--timeout-graceful-shutdown', '2'],
    env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd='.')
failures = []


def get(path, headers=None):
    req = urllib.request.Request(f'http://127.0.0.1:{PORT}{path}',
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except ValueError:
            return e.code, {}


try:
    deadline = time.time() + 30
    up = False
    while time.time() < deadline:
        try:
            code, body = get('/health')
            if code == 200:
                up = True
                break
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.4)
    if not up:
        out = proc.stdout.read().decode() if proc.stdout else ''
        print('ÉCHEC: API non démarrée\n' + out[-2000:])
        sys.exit(1)

    # 1. health signale le verrou
    code, body = get('/health')
    assert code == 200 and body['auth_mode'] == 'locked', body
    assert body['status'] == 'degraded'
    print('OK /health : auth_mode=locked, status=degraded')

    # 2. routes privées → 503 auth_not_configured, même avec une clé
    code, body = get('/v1/usage', {'X-API-Key': 'nimporte'})
    assert code == 503 and body['error']['code'] == 'auth_not_configured', (code, body)
    print('OK /v1/usage sans config : 503 auth_not_configured')

    # multipart minimal
    boundary = 'smoke123'
    payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
               f'filename="a.csv"\r\n\r\nDate;Libelle;Montant\r\n'
               f'01/02/2026;X;10\r\n--{boundary}--\r\n').encode()
    req = urllib.request.Request(
        f'http://127.0.0.1:{PORT}/v1/analyze', data=payload, method='POST',
        headers={'X-API-Key': 'nimporte',
                 'Content-Type': f'multipart/form-data; boundary={boundary}'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 503, code
    print('OK /v1/analyze sans config : 503')

    # 3. arrêt propre (SIGTERM comme Render)
    proc.send_signal(signal.SIGTERM)
    rc = proc.wait(timeout=15)
    print(f'OK arrêt propre (rc={rc})')
    print('SMOKE TEST: TOUT OK')
finally:
    if proc.poll() is None:
        proc.kill()
