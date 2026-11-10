"""Tests de non-régression v0.6.1 — persistance TTL des résultats,
isolation, formats réels, contrat MCP complet contre un VRAI PostgreSQL.

Couverture exigée par l'audit :
1. récupération d'un résultat APRÈS un redémarrage (simulation : purge
   du dictionnaire mémoire _jobs, exactement l'état d'un nouveau
   processus Render) — REST, web (session compte) et MCP ;
2. accès interdit au résultat d'un autre compte (404 indiscernable) ;
3. formats réellement acceptés : .xls binaire refusé EN AMONT par MCP
   (-32602) ; un contenu XLSX renommé .xls reste traité par le moteur ;
4. appels MCP sans clé (401), clé invalide (401), clé valide (200) ;
5. découverte des outils (tools/list = 11 outils annoncés) ;
6. appel de CHACUN des 11 outils (réponses structurées, sans erreur) ;
7. conversion et débit exact (1 crédit = 1 doc tabulaire < 50 lignes) ;
8. rejeu idempotent APRÈS redémarrage (aucun re-débit, aucun crash de
   contrainte unique credit_ledger) ;
9. TTL : résultat réellement purgé à l'expiration (expires_at).

Ces tests exigent pgserver/psycopg (comme test_pg.py) : ignorés sinon.
"""
import base64
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'src'))

if os.environ.get('SF_PG_TEST_URI'):
    pytest.importorskip('psycopg')
    pytest.importorskip('psycopg_pool')
    pgserver = None
else:
    pgserver = pytest.importorskip('pgserver')
    pytest.importorskip('psycopg')
    pytest.importorskip('psycopg_pool')

from statementflow import api as api_mod          # noqa: E402
from statementflow import mcp_http                # noqa: E402
from statementflow.billing import FakeSasPay      # noqa: E402
from statementflow.pg import PgStore, PgUsageStore  # noqa: E402
from statementflow.security import hash_key       # noqa: E402

PGDATA_PARENT = '/tmp/sf-pgserver'
SOCKET_DIR = '/tmp/statementflow'
MIGRATIONS = os.path.join(os.path.dirname(HERE), 'supabase',
                          'migrations')

CSV_OK = (b'Date;Libelle;Debit;Credit;Solde\n'
          b'01/02/2026;A;100;;900\n'
          b'02/02/2026;B;;50;950\n'
          b'03/02/2026;C;10;;940\n')

# signature OLE2 (Compound File) d'un vrai .xls binaire
XLS_OLE2 = bytes.fromhex('d0cf11e0a1b11ae1') + b'\x00' * 512

EXPECTED_TOOLS = {
    'get_account_info', 'get_credits', 'get_supported_formats',
    'get_limits', 'list_conversions', 'get_conversion_status',
    'get_result', 'get_pricing', 'convert_document', 'rotate_api_key',
    'buy_credits',
}


class _ExternalServer:
    def __init__(self, uri):
        self._uri = uri

    def get_uri(self):
        return self._uri

    def cleanup(self):
        pass


@pytest.fixture(scope='session')
def pg_server():
    if os.environ.get('SF_PG_TEST_URI'):
        return _ExternalServer(os.environ['SF_PG_TEST_URI'])
    if os.path.exists('/tmp/sf-pgserver/.s.PGSQL.5432'):
        return pgserver.get_server(PGDATA_PARENT)
    import shutil
    shutil.rmtree(PGDATA_PARENT, ignore_errors=True)
    shutil.rmtree(SOCKET_DIR, ignore_errors=True)
    server = pgserver.get_server(PGDATA_PARENT)
    import atexit
    atexit.register(server.cleanup)
    return server


def _db_uri(base_uri, dbname):
    if '?' in base_uri:
        path, _, query = base_uri.partition('?')
        return path.rstrip('/').rsplit('/', 1)[0] + '/' + dbname + '?' + query
    return base_uri.rstrip('/').rsplit('/', 1)[0] + '/' + dbname


@pytest.fixture(scope='session')
def pg_db_uri(pg_server):
    import psycopg
    uri = pg_server.get_uri()
    with psycopg.connect(uri, autocommit=True) as c:
        if not c.execute(
                "select 1 from pg_database where datname = 'sfv061'"
        ).fetchone():
            c.execute('create database sfv061')
    db_uri = _db_uri(uri, 'sfv061')
    with psycopg.connect(db_uri, autocommit=True) as c:
        # la migration 006 doit passer comme les autres
        for name in sorted(os.listdir(MIGRATIONS)):
            if name.endswith('.sql'):
                with open(os.path.join(MIGRATIONS, name),
                          encoding='utf-8') as f:
                    c.execute(f.read())
    return db_uri


@pytest.fixture()
def app_pg(monkeypatch, pg_db_uri):
    """App complète (REST + web + MCP) branchée sur PostgreSQL, deux
    clés gérées (A: 10 crédits, B: 5 crédits)."""
    from fastapi.testclient import TestClient
    store = PgStore(pg_db_uri, pool_min=0, pool_max=4)
    monkeypatch.setattr(api_mod, 'managed_keys', store)
    monkeypatch.setattr(api_mod, 'billing_store', store)
    monkeypatch.setattr(api_mod, 'usage_store', PgUsageStore(store))
    monkeypatch.setattr(api_mod, '_pg_store', store)
    monkeypatch.setattr(api_mod, 'saspay',
                        FakeSasPay(webhook_secret='whsec-v061'))
    monkeypatch.setattr(api_mod, 'persistence_mode', 'postgres')
    monkeypatch.setenv('STATEMENTFLOW_SASPAY_WEBHOOK_SECRET', 'whsec-v061')
    monkeypatch.setenv('STATEMENTFLOW_TEST_PAYMENT', '1')
    key_a, rec_a = store.issue('client-A', credits=10)
    key_b, rec_b = store.issue('client-B', credits=5)
    api_mod._jobs.clear()
    mcp_http._mcp_limiter = mcp_http.RateLimiter(10_000, 60)
    client = TestClient(api_mod.app)
    yield {'client': client, 'store': store, 'key_a': key_a,
           'key_b': key_b, 'hash_a': rec_a['key_hash'],
           'hash_b': rec_b['key_hash']}
    store.close()


def _restart(app_pg):
    """Simule un redémarrage Render : le dictionnaire mémoire des jobs
    est vide ; PostgreSQL et les crédits persistent. (Le TestClient est
    réutilisé : son transport ne porte aucun état applicatif.)"""
    api_mod._jobs.clear()


def _mcp(client, key):
    """Client MCP minimal (JSON-RPC sur POST /mcp)."""
    class _C:
        def call(self, method, params=None):
            r = client.post('/mcp', headers={
                'Authorization': f'Bearer {key}',
                'Content-Type': 'application/json'},
                json={'jsonrpc': '2.0', 'id': 1, 'method': method,
                      **({'params': params} if params is not None else {})})
            return r

        def tool(self, name, args=None):
            r = self.call('tools/call', {'name': name,
                                         'arguments': args or {}})
            assert r.status_code == 200, r.text
            body = r.json()['result']
            data = json.loads(body['content'][0]['text'])
            return data, body.get('isError') is True
    return _C()


# ── 1. Persistance des résultats après redémarrage ──────────────────
class TestResultatApresRedemarrage:
    def test_rest_result_survit_au_redemarrage(self, app_pg):
        c, store, key = (app_pg['client'], app_pg['store'],
                         app_pg['key_a'])
        r = c.post('/v1/analyze', files={'file': ('t.csv', CSV_OK)},
                   headers={'X-API-Key': key})
        assert r.status_code == 202, r.text
        job_id = r.json()['job_id']
        before = c.get(f'/v1/results/{job_id}',
                       headers={'X-API-Key': key}).json()
        assert before['status'] == 'done'
        assert len(before['transactions']) == 3

        _restart(app_pg)
        after = c.get(f'/v1/results/{job_id}',
                      headers={'X-API-Key': key})
        assert after.status_code == 200, after.text
        body = after.json()
        assert body['status'] == 'done'
        assert body['transactions'] == before['transactions']
        assert body['billing'] == before['billing']

    def test_mcp_result_et_statut_survivent_au_redemarrage(self, app_pg):
        c, key = app_pg['client'], app_pg['key_a']
        mcp = _mcp(c, key)
        data, err = mcp.tool('convert_document', {
            'filename': 'releve.csv',
            'data_base64': base64.b64encode(CSV_OK).decode()})
        assert not err, data
        job_id = data['job_id']

        _restart(app_pg)
        mcp = _mcp(app_pg['client'], key)
        res, err = mcp.tool('get_result', {'job_id': job_id})
        assert not err, res
        assert res['status'] == 'done'
        assert len(res['transactions']) == 3
        st, err = mcp.tool('get_conversion_status', {'job_id': job_id})
        assert not err, st
        assert st['status'] == 'done'
        assert st['transactions_count'] == 3

    def test_erreur_apres_redemarrage_lisible(self, app_pg):
        """Un job en ERREUR reste signalé comme erreur (pas 404 sec)
        après redémarrage : le client sait qu'il n'a pas été débité."""
        c, key = app_pg['client'], app_pg['key_a']
        r = c.post('/v1/analyze',
                   files={'file': ('x.pdf', b'%PDF-fake')},
                   headers={'X-API-Key': key})
        job_id = r.json()['job_id']
        c.get(f'/v1/results/{job_id}', headers={'X-API-Key': key})

        _restart(app_pg)
        r = c.get(f'/v1/results/{job_id}', headers={'X-API-Key': key})
        assert r.status_code == 422  # erreur de traitement, pas 404
        assert r.json()['status'] == 'error'

    def test_web_session_result_survit_au_redemarrage(self, app_pg):
        """Le tableau de bord (session compte) relit aussi depuis la
        base : signup → convert → redémarrage → result."""
        c, store = app_pg['client'], app_pg['store']
        r = c.post('/v1/auth/signup', json={
            'email': 'user-v061@test.dev', 'password': 'MotDePasse42!',
            'accept_terms': True})
        assert r.status_code == 201, r.text
        # vérifier le compte directement (aucun SMTP en test)
        uid = store.get_user_by_email('user-v061@test.dev')['id']
        store.mark_email_verified(uid, via='auto_no_smtp')
        # login
        r = c.post('/v1/auth/login', json={
            'email': 'user-v061@test.dev', 'password': 'MotDePasse42!'})
        assert r.status_code == 200, r.text
        r = c.post('/v1/account/convert',
                   files={'file': ('t.csv', CSV_OK)})
        assert r.status_code == 202, r.text
        job_id = r.json()['job_id']

        _restart(app_pg)
        r = app_pg['client'].get(f'/v1/account/result/{job_id}')
        assert r.status_code == 200, r.text
        assert r.json()['status'] == 'done'
        assert len(r.json()['transactions']) == 3


# ── 2. Isolation entre comptes ──────────────────────────────────────
class TestIsolationInterComptes:
    def test_rest_result_autre_cle_404(self, app_pg):
        c, key_a, key_b = (app_pg['client'], app_pg['key_a'],
                           app_pg['key_b'])
        r = c.post('/v1/analyze', files={'file': ('t.csv', CSV_OK)},
                   headers={'X-API-Key': key_a})
        job_id = r.json()['job_id']
        # clé B : 404, indiscernable d'un job inconnu
        r = c.get(f'/v1/results/{job_id}', headers={'X-API-Key': key_b})
        assert r.status_code == 404
        assert 'error' in r.json()
        # la même pour un job vraiment inconnu
        r = c.get('/v1/results/' + 'f' * 32,
                  headers={'X-API-Key': key_b})
        assert r.status_code == 404

    def test_mcp_result_autre_cle_iserror(self, app_pg):
        c, key_a, key_b = (app_pg['client'], app_pg['key_a'],
                           app_pg['key_b'])
        mcp = _mcp(c, key_a)
        data, _ = mcp.tool('convert_document', {
            'filename': 'a.csv',
            'data_base64': base64.b64encode(CSV_OK).decode()})
        job_id = data['job_id']
        other = _mcp(c, key_b)
        res, err = other.tool('get_result', {'job_id': job_id})
        assert err  # isError: true, « inconnu »
        assert 'inconnu' in res['error'].lower() or \
            'inconnu' in json.dumps(res)

    def test_isolation_apres_redemarrage(self, app_pg):
        """L'isolation tient aussi dans le chemin PostgreSQL (join sur
        key_hash), pas seulement en mémoire."""
        c, key_a, key_b = (app_pg['client'], app_pg['key_a'],
                           app_pg['key_b'])
        r = c.post('/v1/analyze', files={'file': ('t.csv', CSV_OK)},
                   headers={'X-API-Key': key_a})
        job_id = r.json()['job_id']
        _restart(app_pg)
        r = app_pg['client'].get(f'/v1/results/{job_id}',
                                 headers={'X-API-Key': key_b})
        assert r.status_code == 404
        r = app_pg['client'].get(f'/v1/results/{job_id}',
                                 headers={'X-API-Key': key_a})
        assert r.status_code == 200


# ── 3. Formats réellement acceptés ──────────────────────────────────
class TestFormatsReels:
    def test_xls_binaire_refuse_en_amont_mcp(self, app_pg):
        """Un VRAI .xls (OLE2) est refusé AVANT tout traitement ni
        réservation de crédit : -32602, message explicite."""
        c, key = app_pg['client'], app_pg['key_a']
        mcp = _mcp(c, key)
        r = mcp.call('tools/call', {
            'name': 'convert_document',
            'arguments': {
                'filename': 'vieux.xls',
                'data_base64': base64.b64encode(XLS_OLE2).decode()}})
        body = r.json()
        assert body['error']['code'] == -32602
        assert '.xls' in body['error']['message']
        # AUCUN crédit consommé, AUCUN job créé
        credits, _ = mcp.tool('get_credits')
        assert credits['credits_remaining'] == 10

    def test_extensions_acceptees_exactes(self, app_pg):
        """get_supported_formats annonce EXACTEMENT les extensions que
        convert_document accepte (réconciliation audit point 2)."""
        c, key = app_pg['client'], app_pg['key_a']
        mcp = _mcp(c, key)
        data, _ = mcp.tool('get_supported_formats')
        assert data['allowed_extensions'] == ['.pdf', '.csv', '.tsv',
                                              '.xlsx']
        assert '.xls' not in data['allowed_extensions']
        assert 'legacy' in json.dumps(data).lower()

    def test_xlsx_renomme_xls_traite_par_le_moteur(self, app_pg):
        """Le moteur reste indulgent : un contenu XLSX (zip) portant un
        nom .xls EST converti — le refus ne porte que sur le vrai binaire
        OLE2. (Vérification au niveau moteur.)"""
        import openpyxl
        import io
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['Date', 'Libelle', 'Debit', 'Credit', 'Solde'])
        ws.append(['01/02/2026', 'A', 100, None, 900])
        buf = io.BytesIO()
        wb.save(buf)
        from statementflow.engine import parse_bytes
        from statementflow.config import Config
        res = parse_bytes(buf.getvalue(), 'export.xls', Config())
        assert res['document']['kind'] == 'xlsx'
        assert len(res['transactions']) == 1


# ── 4. Authentification MCP ────────────────────────────────────────
class TestAuthMCP:
    def test_sans_cle_401_defi_bearer(self, app_pg):
        r = app_pg['client'].post('/mcp',
                                  headers={'Content-Type':
                                           'application/json'},
                                  json={'jsonrpc': '2.0', 'id': 1,
                                        'method': 'ping'})
        assert r.status_code == 401
        assert r.headers.get('www-authenticate') == 'Bearer'

    def test_cle_invalide_401(self, app_pg):
        r = app_pg['client'].post('/mcp', headers={
            'Authorization': 'Bearer sf_invalide_total',
            'Content-Type': 'application/json'},
            json={'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        assert r.status_code == 401

    def test_cle_valide_200(self, app_pg):
        r = app_pg['client'].post('/mcp', headers={
            'Authorization': f"Bearer {app_pg['key_a']}",
            'Content-Type': 'application/json'},
            json={'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        assert r.status_code == 200
        assert 'result' in r.json()


# ── 5-6. Découverte et appel des 11 outils ──────────────────────────
class TestOnzeOutils:
    def test_decouverte_11_outils(self, app_pg):
        mcp = _mcp(app_pg['client'], app_pg['key_a'])
        r = mcp.call('tools/list')
        names = {t['name'] for t in r.json()['result']['tools']}
        assert names == EXPECTED_TOOLS
        assert len(names) == 11

    def test_chacun_des_11_outils(self, app_pg):
        """Appelle chacun des 11 outils : réponse structurée, pas
        d'erreur interne, schéma cohérent."""
        c, store, key = (app_pg['client'], app_pg['store'],
                         app_pg['key_a'])
        mcp = _mcp(c, key)

        # lecture simple (7)
        for name in ('get_account_info', 'get_credits',
                     'get_supported_formats', 'get_limits',
                     'get_pricing'):
            data, err = mcp.tool(name)
            assert not err, (name, data)

        # conversion (outil 8)
        conv, err = mcp.tool('convert_document', {
            'filename': 'releve.csv',
            'data_base64': base64.b64encode(CSV_OK).decode()})
        assert not err, conv
        job_id = conv['job_id']
        # consultation (outils 6-7 : status + result)
        st, err = mcp.tool('get_conversion_status', {'job_id': job_id})
        assert not err, st
        res, err = mcp.tool('get_result', {'job_id': job_id})
        assert not err, res
        # historique (outil 5)
        hist, err = mcp.tool('list_conversions')
        assert not err, hist
        assert any(rec['job_id'] == job_id for rec in hist['records'])

        # achat de crédits SANS paiement automatique (outil 11)
        buy, err = mcp.tool('buy_credits', {
            'pack': 'starter', 'confirm': True,
            'customer_email': 'acheteur@test.dev',
            'customer_name': 'Acheteur'})
        assert not err, buy
        assert buy['status'] == 'pending'
        assert buy['checkout_url']
        assert 'humain' in buy['note']
        # aucun crédit activé sans paiement
        credits, _ = mcp.tool('get_credits')
        # 10 - 1 (conversion) = 9 ; l'achat n'a rien crédité
        assert credits['credits_remaining'] == 9

        # rotation de clé avec confirmation (outil 10) — sur un VRAI
        # compte client : signup renvoie la clé du compte, c'est ELLE
        # que MCP fait tourner (une clé technique sans compte est
        # refusée : comportement testé séparément)
        r = c.post('/v1/auth/signup', json={
            'email': 'rotate-v061@test.dev', 'password': 'MotDePasse42!',
            'accept_terms': True})
        assert r.status_code == 201, r.text
        account_key = r.json()['api_key']
        acct_mcp = _mcp(c, account_key)
        rot, err = acct_mcp.tool('rotate_api_key', {'confirm': True})
        assert not err, rot
        assert rot['api_key'].startswith('sf_')
        assert rot['credits_transferred'] >= 0  # bonus crédits d'accueil
        # l'ancienne clé du compte est révoquée immédiatement
        r = acct_mcp.call('ping')
        assert r.status_code == 401
        # la nouvelle fonctionne
        new_mcp = _mcp(c, rot['api_key'])
        data, err = new_mcp.tool('get_credits')
        assert not err, data
        assert data['billing_mode'] == 'managed'

        # get_account_info a déjà été appelé (outil 1) ; total = 11/11
        # vérifiés : account_info, credits, formats, limits, pricing,
        # list_conversions, conversion_status, result, convert_document,
        # rotate_api_key, buy_credits.

    def test_rotation_refusee_pour_cle_technique(self, app_pg):
        """Une clé sans compte client (issue en direct) ne peut pas
        tourner depuis MCP : refus explicite — le solde vit sur le
        grand livre de la clé, un compte est nécessaire pour le
        transfert. (Comportement documenté docs/MCP.md.)"""
        c, key = app_pg['client'], app_pg['key_a']
        mcp = _mcp(c, key)
        data, err = mcp.tool('rotate_api_key', {'confirm': True})
        assert err
        assert 'technique' in json.dumps(data) or \
            'compte' in json.dumps(data)
        # la clé fonctionne toujours
        r = mcp.call('ping')
        assert r.status_code == 200

    def test_rotate_sans_confirmation_refuse(self, app_pg):
        mcp = _mcp(app_pg['client'], app_pg['key_a'])
        data, err = mcp.tool('rotate_api_key', {})
        assert err
        assert 'confirm' in json.dumps(data)

    def test_buy_credits_sans_confirmation_refuse(self, app_pg):
        mcp = _mcp(app_pg['client'], app_pg['key_a'])
        data, err = mcp.tool('buy_credits', {'pack': 'pro'})
        assert err
        assert 'confirm' in json.dumps(data)


# ── 7. Débit exact ─────────────────────────────────────────────────
class TestDebitExact:
    def test_un_document_50_lignes_1_credit(self, app_pg):
        """3 lignes de données → 1 crédit exactement (minimum), solde
        10 → 9 ; l'usage est enregistré."""
        c, store, key, hash_a = (app_pg['client'], app_pg['store'],
                                 app_pg['key_a'], app_pg['hash_a'])
        r = c.post('/v1/analyze', files={'file': ('t.csv', CSV_OK)},
                   headers={'X-API-Key': key})
        job_id = r.json()['job_id']
        res = c.get(f'/v1/results/{job_id}',
                    headers={'X-API-Key': key}).json()
        assert res['billing']['credits_charged'] == 1
        assert res['billing']['credits_remaining'] == 9
        assert store.balance(hash_a) == 9
        assert store.usage(hash_a)['documents'] == 1
        assert store.usage(hash_a)['transactions'] == 3

    def test_echec_aucun_debit(self, app_pg):
        c, store, key, hash_a = (app_pg['client'], app_pg['store'],
                                 app_pg['key_a'], app_pg['hash_a'])
        r = c.post('/v1/analyze',
                   files={'file': ('x.pdf', b'%PDF-pas-un-pdf')},
                   headers={'X-API-Key': key})
        job_id = r.json()['job_id']
        r = c.get(f'/v1/results/{job_id}',
                  headers={'X-API-Key': key})
        assert r.status_code == 422
        assert store.balance(hash_a) == 10


# ── 8. Rejeu idempotent après redémarrage ──────────────────────────
class TestRejeuIdempotentApresRedemarrage:
    def test_rejeu_mcp_apres_redemarrage_aucun_redebit(self, app_pg):
        """LE bug corrigé : avant v0.6.1, rejouer la même
        idempotency_key après un redémarrage re-réservait un crédit et
        violait la contrainte unique (key_id, idempotency_key) du grand
        livre → erreur interne. Maintenant : rejeu propre, aucun débit."""
        c, store, key = (app_pg['client'], app_pg['store'],
                         app_pg['key_a'])
        mcp = _mcp(c, key)
        args = {'filename': 'sync.csv',
                'data_base64': base64.b64encode(CSV_OK).decode(),
                'idempotency_key': 'rejeu-2026-11-10-001'}
        first, err = mcp.tool('convert_document', args)
        assert not err, first
        assert first['status'] == 'done'
        job_id = first['job_id']

        _restart(app_pg)
        mcp = _mcp(app_pg['client'], key)
        replay, err = mcp.tool('convert_document', args)
        assert not err, replay
        assert replay['idempotent_replay'] is True
        assert replay['job_id'] == job_id
        assert replay['status'] == 'done'
        # un seul débit au total
        credits, _ = mcp.tool('get_credits')
        assert credits['credits_remaining'] == 9
        # et le grand livre ne contient qu'UNE réservation pour ce job
        kh = app_pg['hash_a']
        recs = store.list_jobs_for_key(kh)
        job_recs = [r for r in recs if r['job_id'] == job_id]
        assert len(job_recs) == 1


# ── 9. TTL : purge réelle à l'expiration ───────────────────────────
class TestTTLPurge:
    def test_resultat_expire_404(self, app_pg, pg_db_uri):
        """Le résultat stocké en base est réellement soumis au TTL :
        après expires_at, plus accessible (ni mémoire ni base)."""
        c, store, key = (app_pg['client'], app_pg['store'],
                         app_pg['key_a'])
        r = c.post('/v1/analyze', files={'file': ('t.csv', CSV_OK)},
                   headers={'X-API-Key': key})
        job_id = r.json()['job_id']

        # vieillir la ligne au-delà du TTL (update direct, comme le
        # ferait le passage du temps — la purge relève de la
        # maintenance, l'accès relève de expires_at)
        import psycopg
        with psycopg.connect(pg_db_uri, autocommit=True) as conn:
            conn.execute(
                'update jobs set expires_at = now() - interval \'1 s\''
                ' where id = %s::uuid', (job_id,))
        api_mod._jobs.clear()  # plus rien en mémoire non plus

        r = c.get(f'/v1/results/{job_id}', headers={'X-API-Key': key})
        assert r.status_code == 404
        assert 'TTL' in r.json()['error']['message']

    def test_purge_expired_jobs_supprime_le_resultat(self, app_pg,
                                                     pg_db_uri):
        """purge_expired_jobs (maintenance horaire) supprime la LIGNE
        complète : le résultat ne dort pas en base au-delà du TTL."""
        c, store, key = (app_pg['client'], app_pg['store'],
                         app_pg['key_a'])
        r = c.post('/v1/analyze', files={'file': ('t.csv', CSV_OK)},
                   headers={'X-API-Key': key})
        job_id = r.json()['job_id']
        import psycopg
        with psycopg.connect(pg_db_uri, autocommit=True) as conn:
            conn.execute(
                'update jobs set expires_at = now() - interval \'1 s\''
                ' where id = %s::uuid', (job_id,))
        purged = store.purge_expired_jobs()
        assert purged >= 1
        with psycopg.connect(pg_db_uri, autocommit=True) as conn:
            row = conn.execute(
                'select count(*) from jobs where id = %s::uuid',
                (job_id,)).fetchone()
        assert row[0] == 0


# ── 10. Contrat REST : échantillon de non-régression ────────────────
class TestContratRest:
    def test_health_et_openapi(self, app_pg):
        c = app_pg['client']
        r = c.get('/health')
        assert r.status_code == 200
        body = r.json()
        assert body['status'] == 'ok'
        assert body['persistence'].startswith('postgres')
        assert body['limits']['results_ttl_s'] == 900

        spec = c.get('/openapi.json').json()
        for route in ('/v1/analyze', '/v1/results/{job_id}', '/v1/usage',
                      '/v1/billing/packs', '/v1/billing/topup',
                      '/v1/billing/status/{reference}', '/health'):
            assert route in spec['paths'], route

    def test_admin_inaccessible_sans_cle_admin(self, app_pg):
        """Les routes d'administration ne sont JAMAIS exposées aux
        clés API ordinaires : 403 même avec une clé client valide."""
        c, key = app_pg['client'], app_pg['key_a']
        for route in ('/v1/admin/usage', '/v1/admin/stats'):
            r = c.get(route, headers={'X-API-Key': key})
            assert r.status_code == 403, (route, r.status_code)
        r = c.post('/v1/admin/usage/reset',
                   headers={'X-API-Key': key},
                   json={'key_hash': 'a' * 64})
        assert r.status_code == 403

    def test_analyze_sans_cle_401(self, app_pg):
        r = app_pg['client'].post(
            '/v1/analyze', files={'file': ('t.csv', CSV_OK)})
        assert r.status_code == 401

    def test_fichier_vide_400_trop_gros_413(self, app_pg, monkeypatch):
        c, key = app_pg['client'], app_pg['key_a']
        r = c.post('/v1/analyze',
                   files={'file': ('v.csv', b'')},
                   headers={'X-API-Key': key})
        assert r.status_code == 400
        monkeypatch.setattr(api_mod.config, 'max_file_mb', 0.001)
        r = c.post('/v1/analyze',
                   files={'file': ('g.csv', b'x' * 4096)},
                   headers={'X-API-Key': key})
        assert r.status_code == 413
