"""Tests sécurité de l'API : matrice d'authentification, isolation des
résultats par clé, administration séparée, quotas, request IDs,
sanitization des noms de fichiers (v0.2.0).

Chaque test vérifie un comportement réel via le TestClient, en mode
fermé (hashes de clés configurés) sauf mention contraire.
"""
import io
import uuid

import pytest

fastapi = pytest.importorskip('fastapi')
from fastapi.testclient import TestClient  # noqa: E402

from statementflow import api as api_mod  # noqa: E402
from statementflow.engine import parse_bytes  # noqa: E402
from statementflow.security import (ApiKeyStore, AdminAuthorizer,  # noqa: E402
                                    hash_key)

GOOD_KEY = 'sk-good-key-1234'
OTHER_KEY = 'sk-other-key-5678'
ADMIN_KEY = 'sk-admin-key-9999'
ADMIN_HASH = hash_key(ADMIN_KEY)

CSV_BYTES = (b'Date;Libelle;Montant;Solde\n'
             b'01/02/2026;ACHAT;100;900\n'
             b'02/02/2026;DEPOT;50;950\n')


@pytest.fixture()
def closed_client(monkeypatch):
    """API en mode fermé : deux clés valides + clé d'administration."""
    monkeypatch.setattr(api_mod, 'api_keys',
                        ApiKeyStore([hash_key(GOOD_KEY), hash_key(OTHER_KEY)]))
    monkeypatch.setattr(api_mod, 'admin_auth',
                        AdminAuthorizer(ADMIN_HASH))
    api_mod._jobs.clear()
    return TestClient(api_mod.app)


@pytest.fixture()
def open_client(monkeypatch):
    """API en mode ouvert (développement local, défaut sans config)."""
    monkeypatch.setattr(api_mod, 'api_keys', ApiKeyStore([]))
    monkeypatch.setattr(api_mod, 'admin_auth', AdminAuthorizer())
    api_mod._jobs.clear()
    return TestClient(api_mod.app)


def _submit(client, key, data=CSV_BYTES, filename='doc.csv'):
    return client.post('/v1/analyze',
                       files={'file': (filename, io.BytesIO(data))},
                       headers={'X-API-Key': key})


class TestAuth401:
    """Toute route privée sans clé ou avec mauvaise clé → 401."""

    def test_analyze_sans_cle(self, closed_client):
        r = closed_client.post('/v1/analyze',
                               files={'file': ('a.csv', io.BytesIO(CSV_BYTES))})
        assert r.status_code == 401
        assert r.json()['error']['code'] == 'unauthorized'

    def test_analyze_cle_vide(self, closed_client):
        r = _submit(closed_client, '')
        assert r.status_code == 401

    def test_analyze_cle_inconnue(self, closed_client):
        r = _submit(closed_client, 'sk-wrong-key')
        assert r.status_code == 401
        # l'erreur ne doit rien révéler sur les clés configurées
        assert GOOD_KEY not in r.text and OTHER_KEY not in r.text

    def test_results_sans_cle(self, closed_client):
        job = _submit(closed_client, GOOD_KEY).json()
        r = closed_client.get(f"/v1/results/{job['job_id']}")
        assert r.status_code == 401

    def test_results_cle_inconnue(self, closed_client):
        job = _submit(closed_client, GOOD_KEY).json()
        r = closed_client.get(f"/v1/results/{job['job_id']}",
                              headers={'X-API-Key': 'sk-wrong'})
        assert r.status_code == 401

    def test_usage_sans_cle(self, closed_client):
        assert closed_client.get('/v1/usage').status_code == 401

    def test_admin_sans_cle(self, closed_client):
        assert closed_client.get('/v1/admin/usage').status_code == 403
        assert closed_client.post('/v1/admin/usage/reset',
                                  json={'key_hash': 'x'}).status_code == 403


class TestAuth200:
    """Clé valide → accès autorisé ; mode ouvert documenté."""

    def test_analyze_cle_valide(self, closed_client):
        r = _submit(closed_client, GOOD_KEY)
        assert r.status_code == 202
        assert r.json()['status'] in ('done', 'processing')

    def test_mode_ouvert_accepte_toute_cle_non_vide(self, open_client):
        assert _submit(open_client, 'nimporte-quelle-cle').status_code == 202

    def test_mode_ouvert_refuse_cle_vide(self, open_client):
        assert _submit(open_client, '').status_code == 401

    def test_health_public_sans_cle(self, closed_client):
        r = closed_client.get('/health')
        assert r.status_code == 200
        assert r.json()['auth_mode'] == 'key-hash-list'


class TestIsolationResultats:
    """Un résultat n'est lisible que par la clé qui a soumis le document."""

    def test_proprietaire_accede(self, closed_client):
        job = _submit(closed_client, GOOD_KEY).json()
        r = closed_client.get(f"/v1/results/{job['job_id']}",
                              headers={'X-API-Key': GOOD_KEY})
        assert r.status_code == 200 and r.json()['status'] == 'done'

    def test_autre_cle_refusee_404_indistinguishable(self, closed_client):
        """Autre clé VALIDE → 404, indistinguable d'un job inconnu
        (pas de sonde d'existence)."""
        job = _submit(closed_client, GOOD_KEY).json()
        r_other = closed_client.get(f"/v1/results/{job['job_id']}",
                                    headers={'X-API-Key': OTHER_KEY})
        r_unknown = closed_client.get('/v1/results/' + '0' * 32,
                                      headers={'X-API-Key': OTHER_KEY})
        assert r_other.status_code == 404
        assert r_unknown.status_code == 404
        # corps identiques (code + message) — le request_id est unique par
        # requête par conception
        e1, e2 = r_other.json()['error'], r_unknown.json()['error']
        assert e1['code'] == e2['code'] == 'not_found'
        assert e1['message'] == e2['message']

    def test_usage_ne_montre_que_sa_cle(self, closed_client):
        from statementflow.security import hash_key as hk
        api_mod.usage_store.reset(hk(GOOD_KEY))
        api_mod.usage_store.reset(hk(OTHER_KEY))
        _submit(closed_client, GOOD_KEY)
        r = closed_client.get('/v1/usage', headers={'X-API-Key': OTHER_KEY})
        assert r.json()['documents'] == 0
        r2 = closed_client.get('/v1/usage', headers={'X-API-Key': GOOD_KEY})
        assert r2.json()['documents'] == 1

    def test_cle_jamais_en_clair_dans_les_reponses(self, closed_client):
        job = _submit(closed_client, GOOD_KEY).json()
        r_res = closed_client.get(f"/v1/results/{job['job_id']}",
                                  headers={'X-API-Key': GOOD_KEY})
        r_use = closed_client.get('/v1/usage', headers={'X-API-Key': GOOD_KEY})
        for r in (r_res, r_use):
            assert GOOD_KEY not in r.text
        # le résultat n'expose même pas le hash ; l'usage l'expose par design
        assert hash_key(GOOD_KEY) not in r_res.text
        assert hash_key(GOOD_KEY) in r_use.text


class TestAdmin:
    """Administration séparée : clé admin DISTINCTE, réinitialisation."""

    def test_reset_avec_cle_api_simple_refuse(self, closed_client):
        r = closed_client.post('/v1/admin/usage/reset',
                               json={'key_hash': 'x'},
                               headers={'X-API-Key': GOOD_KEY})
        assert r.status_code == 403

    def test_admin_liste_hash_uniquement(self, closed_client):
        _submit(closed_client, GOOD_KEY)
        r = closed_client.get('/v1/admin/usage',
                              headers={'X-Admin-Key': ADMIN_KEY})
        assert r.status_code == 200
        text = r.text
        assert GOOD_KEY not in text and OTHER_KEY not in text
        assert hash_key(GOOD_KEY) in text

    def test_admin_reset_fonctionne(self, closed_client):
        _submit(closed_client, GOOD_KEY)
        h = hash_key(GOOD_KEY)
        r = closed_client.post('/v1/admin/usage/reset',
                               json={'key_hash': h},
                               headers={'X-Admin-Key': ADMIN_KEY})
        assert r.status_code == 200 and r.json()['reset'] is True
        after = closed_client.get('/v1/usage', headers={'X-API-Key': GOOD_KEY})
        assert after.json()['documents'] == 0

    def test_delete_anonyme_supprime(self, closed_client):
        """L'ancienne route DELETE /v1/usage/{key} (réinitialisation
        anonyme) n'existe plus → 405."""
        r = closed_client.delete('/v1/usage/whatever')
        assert r.status_code == 404  # route supprimée : plus jamais 200


class TestQuotasEtLimites:
    def test_quota_documents_par_cle(self, closed_client, monkeypatch):
        from statementflow.security import hash_key as hk
        monkeypatch.setattr(api_mod.quota, 'max_documents', 1)
        key = f'quota-{uuid.uuid4().hex[:8]}'
        # mode fermé : la clé de quota doit être déclarée valide
        monkeypatch.setattr(
            api_mod, 'api_keys',
            ApiKeyStore([hash_key(GOOD_KEY), hash_key(OTHER_KEY),
                         hash_key(key)]))
        assert _submit(closed_client, key).status_code == 202
        r = _submit(closed_client, key)
        assert r.status_code == 429
        assert r.json()['error']['code'] == 'quota_exceeded'
        api_mod.usage_store.reset(hk(key))

    def test_fichier_trop_gros_structure(self, closed_client, monkeypatch):
        monkeypatch.setattr(api_mod.config, 'max_file_mb', 0.001)
        r = _submit(closed_client, GOOD_KEY, data=b'x' * 2048)
        assert r.status_code == 413
        assert r.json()['error']['code'] == 'file_too_large'


class TestRequestIdsEtSanitization:
    def test_request_id_present_et_echo(self, closed_client):
        r = closed_client.get('/health', headers={'X-Request-ID': 'mon-id-42'})
        assert r.headers.get('X-Request-ID') == 'mon-id-42'
        r2 = closed_client.get('/health')
        assert r2.headers.get('X-Request-ID')

    def test_erreur_contient_request_id(self, closed_client):
        r = closed_client.post('/v1/analyze',
                               files={'file': ('a.csv', io.BytesIO(CSV_BYTES))})
        assert r.json()['error']['request_id']
        assert r.headers['X-Request-ID'] == r.json()['error']['request_id']

    def test_nom_de_fichier_nettoye(self, closed_client):
        r = _submit(closed_client, GOOD_KEY,
                    filename='../../etc/passwd.csv')
        assert r.status_code == 202
        result = closed_client.get(f"/v1/results/{r.json()['job_id']}",
                                   headers={'X-API-Key': GOOD_KEY}).json()
        assert result['document']['filename'] == 'passwd.csv'
        assert '/' not in result['document']['filename']
        assert '..' not in result['document']['filename']

    def test_nom_de_fichier_controle_chars(self, closed_client):
        r = _submit(closed_client, GOOD_KEY,
                    filename='bad\x01\x02name\n.csv')
        result = closed_client.get(f"/v1/results/{r.json()['job_id']}",
                                   headers={'X-API-Key': GOOD_KEY}).json()
        assert all(c.isprintable() for c in result['document']['filename'])


class TestLimitesMoteur:
    def test_zip_bomb_xlsx_plafonne(self):
        """XLSX dépassant max_rows → ParseError (plafond pendant la lecture)."""
        from statementflow.config import Config
        from statementflow.engine import ParseError
        import openpyxl
        buf = io.BytesIO()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['Date', 'Libelle', 'Montant', 'Solde'])
        for i in range(60):
            ws.append([f'01/02/2026', f'L{i}', 10, i])
        wb.save(buf)
        cfg = Config(max_rows=50)
        with pytest.raises(ParseError, match='limite'):
            parse_bytes(buf.getvalue(), 'gros.xlsx', cfg)
