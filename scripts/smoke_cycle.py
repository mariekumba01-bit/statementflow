"""Smoke test du cycle nominal de l'API (v0.4.0) — simule l'usage
Render réel : démarrage uvicorn → /health → upload multipart
(python-multipart !) → résultat avec usage.rows → catalogue packs.

Différence avec smoke_render.py (qui vérifie le VERROU) : celui-ci
vérifie le FONCTIONNEMENT en mode ouvert explicite de développement
local (STATEMENTFLOW_ALLOW_OPEN_AUTH=1), exactement le parcours qui
crashait sur Render avant l'ajout de python-multipart.

Exécution : python scripts/smoke_cycle.py — code de sortie 0 = OK.
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = '8298'


def get(path, headers=None, timeout=15):
    req = urllib.request.Request(f'http://127.0.0.1:{PORT}{path}',
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main():
    env = dict(os.environ)
    env.update({
        'PORT': PORT,
        'STATEMENTFLOW_API_HOST': '127.0.0.1',
        # mode ouvert EXPLICITE (développement local isolé) pour tester
        # le parcours nominal sans clés gérées
        'STATEMENTFLOW_ALLOW_OPEN_AUTH': '1',
    })
    proc = subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'statementflow.api:app',
         '--host', '127.0.0.1', '--port', PORT,
         '--timeout-graceful-shutdown', '2'],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 30
        up = False
        while time.time() < deadline:
            try:
                code, _ = get('/health', timeout=2)
                if code == 200:
                    up = True
                    break
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            time.sleep(0.4)
        if not up:
            out = proc.stdout.read().decode() if proc.stdout else ''
            print('ÉCHEC: API non démarrée\n' + out[-2000:])
            return 1
        code, body = get('/health')
        health = json.loads(body)
        assert health['auth_mode'] == 'open' \
            and health['persistence'] == 'json', health
        print(f"OK /health : v{health['version']} open/json")

        # upload multipart — LE crash Render historique
        boundary = 'cycle456'
        csv = (b'Date;Libelle;Debit;Credit;Solde\n'
               b'01/02/2026;Achat;100;;900\n'
               b'02/02/2026;Vente;;50;950\n')
        payload = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="releve.csv"\r\nContent-Type: text/csv\r\n\r\n'
        ).encode() + csv + f'\r\n--{boundary}--\r\n'.encode()
        req = urllib.request.Request(
            f'http://127.0.0.1:{PORT}/v1/analyze', data=payload,
            method='POST',
            headers={'X-API-Key': 'smoke',
                     'Content-Type':
                         f'multipart/form-data; boundary={boundary}'})
        with urllib.request.urlopen(req, timeout=30) as r:
            code, body = r.status, r.read()
        assert code == 202, (code, body[:200])
        job = json.loads(body)['job_id']
        print(f'OK /v1/analyze (multipart) : job {job[:12]}…')

        for _ in range(30):
            code, body = get(f'/v1/results/{job}',
                             headers={'X-API-Key': 'smoke'})
            if code != 202:
                break
            time.sleep(0.3)
        assert code == 200, (code, body[:300])
        result = json.loads(body)
        assert len(result['transactions']) == 2, result['usage']
        assert result['usage']['rows'] == 3, result['usage']
        assert result['billing']['billing_mode'] == 'unmanaged'
        print(f"OK /v1/results : 2 transactions, usage={result['usage']}")

        code, body = get('/v1/billing/packs')
        packs = json.loads(body)
        assert code == 200 and set(packs['packs'][0]) >= {'pack',
                                                          'credits'}
        print('OK /v1/billing/packs : catalogue public')

        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=15)
        print(f'OK arrêt propre (rc={rc})')
        print('SMOKE CYCLE: TOUT OK')
        return 0
    finally:
        if proc.poll() is None:
            proc.kill()


if __name__ == '__main__':
    sys.exit(main())
