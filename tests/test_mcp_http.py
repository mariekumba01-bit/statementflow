"""Tests pytest du serveur MCP distant StatementFlow (Streamable HTTP,
POST /mcp) — transport, authentification, isolation, facturation.

Contrairement à tests/test_mcp.py (serveur stdio local, moteur sans
compte), ces tests couvrent le serveur MONTÉ DANS l'app FastAPI : ils
exercent le vrai transport HTTP, la négociation de protocole et le
chemin financier (réserve → settle/refund) via JSON-RPC.

Aucun paiement réel : la passerelle est le simulateur FakeSasPay (mêmes
contrats que SasPay), les webhooks sont signés avec un secret de test.
"""
import base64
import io
import json
import os
import threading

import pytest

fastapi = pytest.importorskip('fastapi')
from fastapi.testclient import TestClient  # noqa: E402

from statementflow import api as api_mod  # noqa: E402
from statementflow import mcp_http  # noqa: E402
from statementflow.billing import BillingStore, FakeSasPay  # noqa: E402
from statementflow.security import ManagedKeyStore, hash_key  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, 'data', 'corpus')
WEBHOOK_SECRET = 'whsec-mcp-tests'

WAVE_CSV = os.path.join(CORPUS, 'SYNTH_csv_wave_v2_U.csv')
ORANGE_CSV = os.path.join(CORPUS, 'SYNTH_csv_orange_money_V.csv')

EXPECTED_TOOLS = {
    'get_account_info', 'get_credits', 'get_supported_formats',
    'get_limits', 'list_conversions', 'get_conversion_status',
    'get_result', 'get_pricing', 'convert_document', 'rotate_api_key',
    'buy_credits',
}


@pytest.fixture()
def mcp_env(monkeypatch, tmp_path):
    """App FastAPI avec magasins JSON temporaires + FakeSasPay, deux
    clés gérées (A solde 10, B solde 5) — même dispositif que les tests
    de facturation existants."""
    store = ManagedKeyStore(str(tmp_path / 'keys.json'))
    key_a, _ = store.issue('client-A', credits=10)
    key_b, _ = store.issue('client-B', credits=5)
    monkeypatch.setattr(api_mod, 'managed_keys', store)
    monkeypatch.setattr(api_mod, 'billing_store',
                        BillingStore(str(tmp_path / 'billing.json')))
    gateway = FakeSasPay(webhook_secret=WEBHOOK_SECRET)
    monkeypatch.setattr(api_mod, 'saspay', gateway)
    monkeypatch.setenv('STATEMENTFLOW_SASPAY_WEBHOOK_SECRET', WEBHOOK_SECRET)
    api_mod._jobs.clear()
    mcp_http._mcp_limiter = mcp_http.RateLimiter(1000, 60)
    client = TestClient(api_mod.app)
    return SimpleNamespace(client=client, key_a=key_a, key_b=key_b,
                           store=store, gateway=gateway)


class SimpleNamespace:  # évite l'import de types pour compat 3.10
    def __init__(self, **kw):
        self.__dict__.update(kw)


class MCP:
    """Petit client JSON-RPC sur le transport HTTP testé."""

    def __init__(self, http, key=None):
        self.http = http
        self.key = key
        self._id = 0

    def raw(self, payload, key='(keep)', headers=None, content_type=None,
            method='post'):
        h = dict(headers or {})
        if content_type:
            h['Content-Type'] = content_type
        if key != '(unset)' and 'Authorization' not in h \
                and 'X-API-Key' not in h:
            h['Authorization'] = f'Bearer {self.key if key == "(keep)" else key}'
        if content_type is None:
            h['Content-Type'] = 'application/json'
        caller = getattr(self.http, method)
        return caller('/mcp', content=json.dumps(payload)
                      if not isinstance(payload, bytes) else payload,
                      headers=h)

    def call(self, method_, params=None, key='(keep)'):
        self._id += 1
        msg = {'jsonrpc': '2.0', 'id': self._id, 'method': method_}
        if params is not None:
            msg['params'] = params
        r = self.raw(msg, key=key)
        assert r.status_code == 200, r.text
        return r.json()

    def tool(self, name, arguments=None, key='(keep)'):
        r = self.call('tools/call', {'name': name,
                                     'arguments': arguments or {}},
                      key=key)
        assert 'result' in r, r
        res = r['result']
        assert res.get('content', [{}])[0].get('type') == 'text'
        data = json.loads(res['content'][0]['text'])
        assert data == res.get('structuredContent') or \
            res.get('structuredContent') is None
        return data, res.get('isError') is True

    def result(self, method_, params=None, key='(keep)'):
        return self.call(method_, params, key=key)['result']


def _b64(path):
    with open(path, 'rb') as f:
        return base64.b64encode(f.read()).decode()


class TestTransportEtDecouverte:
    """Compatibilité du transport Streamable HTTP et découverte MCP."""

    def test_initialize_negocie_la_version(self, mcp_env):
        mcp = MCP(mcp_env.client, mcp_env.key_a)
        r = mcp.result('initialize', {
            'protocolVersion': '2025-06-18', 'capabilities': {},
            'clientInfo': {'name': 'pytest', 'version': '0'}})
        assert r['protocolVersion'] == '2025-06-18'
        assert r['serverInfo']['name'] == 'statementflow'
        assert 'tools' in r['capabilities']
        assert 'resources' not in r['capabilities']

    def test_initialize_version_inconnue_replie_defaut(self, mcp_env):
        mcp = MCP(mcp_env.client, mcp_env.key_a)
        r = mcp.result('initialize', {
            'protocolVersion': '1999-01-01', 'capabilities': {},
            'clientInfo': {'name': 'pytest', 'version': '0'}})
        assert r['protocolVersion'] == mcp_http.DEFAULT_PROTOCOL_VERSION

    def test_tools_list_sans_initialize(self, mcp_env):
        """Serveur stateless : tools/list doit marcher directement
        (compatibilité clients distants qui ne renvoient pas
        initialize à chaque connexion)."""
        mcp = MCP(mcp_env.client, mcp_env.key_a)
        tools = mcp.result('tools/list')['tools']
        names = {t['name'] for t in tools}
        assert names == EXPECTED_TOOLS
        for t in tools:
            assert t['description']
            assert t['inputSchema']['type'] == 'object'
        ann = {t['name']: t.get('annotations', {}) for t in tools}
        assert ann['rotate_api_key'].get('destructiveHint') is True
        assert ann['get_credits'].get('readOnlyHint') is True

    def test_ping(self, mcp_env):
        mcp = MCP(mcp_env.client, mcp_env.key_a)
        assert mcp.result('ping') == {}

    def test_notification_repond_202_sans_corps(self, mcp_env):
        r = mcp_env.client.post(
            '/mcp', content=json.dumps(
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'}),
            headers={'Authorization': f'Bearer {mcp_env.key_a}',
                     'Content-Type': 'application/json'})
        assert r.status_code == 202
        assert r.content == b''

    def test_batch_refuse(self, mcp_env):
        mcp = MCP(mcp_env.client, mcp_env.key_a)
        r = mcp.raw([{'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}])
        assert r.status_code == 200
        assert r.json()['error']['code'] == -32600

    def test_json_invalide_parse_error(self, mcp_env):
        r = mcp_env.client.post(
            '/mcp', content=b'{pas du json',
            headers={'Authorization': f'Bearer {mcp_env.key_a}',
                     'Content-Type': 'application/json'})
        assert r.status_code == 200
        assert r.json()['error']['code'] == -32700

    def test_content_type_non_json_415(self, mcp_env):
        r = MCP(mcp_env.client, mcp_env.key_a).raw(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
            content_type='text/plain')
        assert r.status_code == 415

    def test_version_protocole_header_invalide_400(self, mcp_env):
        r = MCP(mcp_env.client, mcp_env.key_a).raw(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
            headers={'MCP-Protocol-Version': '2000-01-01'})
        assert r.status_code == 400

    def test_version_protocole_header_valide_acceptee(self, mcp_env):
        r = MCP(mcp_env.client, mcp_env.key_a).raw(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
            headers={'MCP-Protocol-Version': '2025-03-26'})
        assert r.status_code == 200
        assert 'result' in r.json()

    def test_get_et_delete_405(self, mcp_env):
        for method in ('get', 'delete'):
            r = getattr(mcp_env.client, method)(
                '/mcp', headers={'Authorization': f'Bearer {mcp_env.key_a}'})
            assert r.status_code == 405
            assert r.headers['allow'] == 'POST'

    def test_methode_inconnue(self, mcp_env):
        mcp = MCP(mcp_env.client, mcp_env.key_a)
        r = mcp.call('resources/list')
        assert r['error']['code'] == -32601


class TestAuthentification:
    def test_sans_cle_401_avec_defi_bearer(self, mcp_env):
        r = MCP(mcp_env.client).raw(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}, key='(unset)')
        assert r.status_code == 401
        assert r.headers.get('www-authenticate') == 'Bearer'

    def test_cle_invalide_401(self, mcp_env):
        r = MCP(mcp_env.client).raw(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}, key='sf_mauvaise')
        assert r.status_code == 401

    def test_cle_revoquee_401(self, mcp_env):
        mcp_env.store.revoke(hash_key(mcp_env.key_b))
        r = MCP(mcp_env.client, mcp_env.key_b).raw(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        assert r.status_code == 401

    def test_auth_via_x_api_key_aussi(self, mcp_env):
        r = mcp_env.client.post(
            '/mcp', content=json.dumps(
                {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}),
            headers={'X-API-Key': mcp_env.key_a,
                     'Content-Type': 'application/json'})
        assert r.status_code == 200
        assert 'result' in r.json()


class TestOutilsLecture:
    def test_get_account_info_ne_revele_pas_la_cle(self, mcp_env):
        mcp = MCP(mcp_env.client, mcp_env.key_a)
        data, err = mcp.tool('get_account_info')
        assert not err
        assert data['billing_mode'] == 'managed'
        assert mcp_env.key_a not in json.dumps(data)
        assert data['key_masked'].endswith('****')

    def test_get_credits_solde_exact(self, mcp_env):
        data, err = MCP(mcp_env.client, mcp_env.key_a).tool('get_credits')
        assert not err
        assert data['credits_remaining'] == 10
        assert data['billing_mode'] == 'managed'

    def test_get_supported_formats(self, mcp_env):
        data, err = MCP(mcp_env.client,
                        mcp_env.key_a).tool('get_supported_formats')
        assert not err
        assert {f['format'] for f in data['input_formats']} == \
            {'pdf', 'csv', 'tsv', 'xlsx'}
        assert any('Wave' in s for s in data['mobile_money_sources'])

    def test_get_limits(self, mcp_env):
        data, err = MCP(mcp_env.client, mcp_env.key_a).tool('get_limits')
        assert not err
        assert data['max_file_mb'] > 0
        assert data['results_ttl_s'] > 0

    def test_get_pricing_catalogue_serveur(self, mcp_env):
        data, err = MCP(mcp_env.client, mcp_env.key_a).tool('get_pricing')
        assert not err
        packs = {p['pack']: p for p in data['packs']}
        assert packs['starter']['amount_fcfa'] == 5000
        assert packs['business']['credits'] == 10000

    def test_list_conversions_vide_puis_pleine(self, mcp_env):
        mcp = MCP(mcp_env.client, mcp_env.key_a)
        data, _ = mcp.tool('list_conversions')
        assert data['records'] == []
        mcp.tool('convert_document', {
            'filename': 'releve.csv', 'data_base64': _b64(WAVE_CSV)})
        data, _ = mcp.tool('list_conversions')
        assert len(data['records']) == 1
        assert data['records'][0]['status'] == 'done'


class TestConversionEtFacturation:
    def test_conversion_reelle_et_decompte(self, mcp_env):
        mcp = MCP(mcp_env.client, mcp_env.key_a)
        data, err = mcp.tool('convert_document', {
            'filename': 'releve-wave.csv', 'data_base64': _b64(WAVE_CSV)})
        assert not err, data
        assert data['status'] == 'done'
        assert data['transactions_count'] > 0
        assert data['billing']['credits_charged'] == 1
        assert data['billing']['credits_remaining'] == 9
        # solde confirmé par l'outil de lecture
        credits, _ = mcp.tool('get_credits')
        assert credits['credits_remaining'] == 9
        # résultat complet récupérable
        res, err = mcp.tool('get_result', {'job_id': data['job_id']})
        assert not err
        assert len(res['transactions']) == data['transactions_count']
        assert res['usage']['transactions'] == data['transactions_count']
        # statut
        st, _ = mcp.tool('get_conversion_status', {'job_id': data['job_id']})
        assert st['status'] == 'done'
        assert st['transactions_count'] == data['transactions_count']

    def test_conversion_avec_currency_hint(self, mcp_env):
        data, err = MCP(mcp_env.client, mcp_env.key_b).tool(
            'convert_document',
            {'filename': 'orange.csv', 'data_base64': _b64(ORANGE_CSV),
             'currency_hint': 'XOF'})
        assert not err, data
        res, _ = MCP(mcp_env.client, mcp_env.key_b).tool(
            'get_result', {'job_id': data['job_id']})
        assert res['currency'] == 'XOF'

    def test_idempotence_rejeu_meme_cle(self, mcp_env):
        mcp = MCP(mcp_env.client, mcp_env.key_a)
        args = {'filename': 'releve.csv', 'data_base64': _b64(WAVE_CSV),
                'idempotency_key': 'sync-2026-11-09-001'}
        first, err = mcp.tool('convert_document', args)
        assert not err and first['status'] == 'done'
        replay, err = mcp.tool('convert_document', args)
        assert not err
        assert replay['idempotent_replay'] is True
        assert replay['job_id'] == first['job_id']
        credits, _ = mcp.tool('get_credits')
        assert credits['credits_remaining'] == 9  # UN seul débit

    def test_idempotence_isolee_par_cle(self, mcp_env):
        """La même idempotency_key sous une autre clé produit un job
        DIFFÉRENT (dérivation HMAC liée à la clé)."""
        a = MCP(mcp_env.client, mcp_env.key_a)
        b = MCP(mcp_env.client, mcp_env.key_b)
        args = {'filename': 'releve.csv', 'data_base64': _b64(WAVE_CSV),
                'idempotency_key': 'cle-partagee-123'}
        ra, _ = a.tool('convert_document', args)
        rb, _ = b.tool('convert_document', args)
        assert ra['job_id'] != rb['job_id']
        assert ra['status'] == 'done' and rb['status'] == 'done'

    def test_echec_rembourse_tout(self, mcp_env):
        mcp = MCP(mcp_env.client, mcp_env.key_a)
        data, err = mcp.tool('convert_document', {
            'filename': ' faux.pdf',
            'data_base64': base64.b64encode(b'ceci n est pas un pdf').decode()})
        assert err  # isError: true
        assert data['status'] == 'error'
        assert data['credits_charged'] == 0
        credits, _ = mcp.tool('get_credits')
        assert credits['credits_remaining'] == 10  # rien consommé

    def test_fichier_trop_volumineux_refuse(self, mcp_env, monkeypatch):
        monkeypatch.setattr(api_mod.config, 'max_file_mb', 0.001)
        data, err = MCP(mcp_env.client, mcp_env.key_a).tool(
            'convert_document',
            {'filename': 'gros.csv',
             'data_base64': base64.b64encode(b'x' * 4096).decode()})
        assert err
        assert 'volumineux' in json.dumps(data)

    def test_credits_epuises_refus(self, mcp_env):
        mcp_env.store.add_credits(hash_key(mcp_env.key_b), -5)  # solde 0
        data, err = MCP(mcp_env.client, mcp_env.key_b).tool(
            'convert_document',
            {'filename': 'r.csv', 'data_base64': _b64(WAVE_CSV)})
        assert err
        assert 'épuisés' in data['error']

    def test_concurrence_un_credit_une_conversion(self, mcp_env):
        """8 appels simultanés sur un solde de 1 crédit : exactement
        1 conversion réussie, 7 refusées — atomicité de la réservation
        via le chemin financier existant."""
        mcp_env.store.add_credits(hash_key(mcp_env.key_b), -4)  # solde 1
        results: list[tuple] = []
        barrier = threading.Barrier(8)

        def submit():
            mcp = MCP(mcp_env.client, mcp_env.key_b)
            barrier.wait()
            data, err = mcp.tool(
                'convert_document',
                {'filename': 'r.csv', 'data_base64': _b64(WAVE_CSV)})
            results.append((data.get('status'), err))

        threads = [threading.Thread(target=submit) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        done = [r for r in results if r[0] == 'done']
        refused = [r for r in results if r[1]]
        assert len(done) == 1
        assert len(refused) == 7


class TestErreursEtValidation:
    def test_outil_inconnu(self, mcp_env):
        r = MCP(mcp_env.client, mcp_env.key_a).call(
            'tools/call', {'name': 'admin_stats', 'arguments': {}})
        assert r['error']['code'] == -32602
        assert 'admin_stats' in r['error']['message']

    def test_aucun_outil_admin_expose(self, mcp_env):
        tools = MCP(mcp_env.client,
                    mcp_env.key_a).result('tools/list')['tools']
        for t in tools:
            assert 'admin' not in t['name'].lower()

    def test_job_inconnu_iserror(self, mcp_env):
        data, err = MCP(mcp_env.client, mcp_env.key_a).tool(
            'get_result', {'job_id': 'f' * 32})
        assert err
        assert 'inconnu' in data['error']

    def test_job_id_malforme(self, mcp_env):
        r = MCP(mcp_env.client, mcp_env.key_a).call(
            'tools/call', {'name': 'get_result',
                           'arguments': {'job_id': 'xyz'}})
        assert r['error']['code'] == -32602

    def test_extension_interdite(self, mcp_env):
        r = MCP(mcp_env.client, mcp_env.key_a).call(
            'tools/call', {'name': 'convert_document',
                           'arguments': {'filename': 'doc.exe',
                                         'data_base64': 'AAAA'}})
        assert r['error']['code'] == -32602

    def test_base64_invalide(self, mcp_env):
        r = MCP(mcp_env.client, mcp_env.key_a).call(
            'tools/call', {'name': 'convert_document',
                           'arguments': {'filename': 'doc.csv',
                                         'data_base64': '!!!pasb64!!!'}})
        assert r['error']['code'] == -32602

    def test_argument_manquant(self, mcp_env):
        r = MCP(mcp_env.client, mcp_env.key_a).call(
            'tools/call', {'name': 'convert_document',
                           'arguments': {'filename': 'doc.csv'}})
        assert r['error']['code'] == -32602

    def test_params_non_objet(self, mcp_env):
        r = MCP(mcp_env.client, mcp_env.key_a).call(
            'tools/call', [1, 2])
        assert r['error']['code'] == -32600

    def test_limit_invalide(self, mcp_env):
        r = MCP(mcp_env.client, mcp_env.key_a).call(
            'tools/call', {'name': 'list_conversions',
                           'arguments': {'limit': 5000}})
        assert r['error']['code'] == -32602


class TestIsolationEntreComptes:
    def test_resultat_d_un_autre_compte_inconnu(self, mcp_env):
        a = MCP(mcp_env.client, mcp_env.key_a)
        conv, _ = a.tool('convert_document', {
            'filename': 'r.csv', 'data_base64': _b64(WAVE_CSV)})
        # B ne voit ni le résultat, ni le statut — message identique à
        # un job inexistant (pas de sonde d'existence)
        data_b, err_b = MCP(mcp_env.client, mcp_env.key_b).tool(
            'get_result', {'job_id': conv['job_id']})
        assert err_b
        st_b, err_st = MCP(mcp_env.client, mcp_env.key_b).tool(
            'get_conversion_status', {'job_id': conv['job_id']})
        assert err_st
        data_none, err_none = MCP(mcp_env.client, mcp_env.key_b).tool(
            'get_result', {'job_id': 'a' * 32})
        assert data_b['error'] == data_none['error']

    def test_historique_isole(self, mcp_env):
        a = MCP(mcp_env.client, mcp_env.key_a)
        a.tool('convert_document', {
            'filename': 'r.csv', 'data_base64': _b64(WAVE_CSV)})
        hist_b, _ = MCP(mcp_env.client, mcp_env.key_b).tool(
            'list_conversions')
        assert hist_b['records'] == []

    def test_debit_isole(self, mcp_env):
        a = MCP(mcp_env.client, mcp_env.key_a)
        a.tool('convert_document', {
            'filename': 'r.csv', 'data_base64': _b64(WAVE_CSV)})
        credits_b, _ = MCP(mcp_env.client, mcp_env.key_b).tool('get_credits')
        assert credits_b['credits_remaining'] == 5  # B intact


class TestActionsConfirmees:
    def test_rotation_sans_confirmation_refusee(self, mcp_env):
        data, err = MCP(mcp_env.client, mcp_env.key_a).tool(
            'rotate_api_key', {})
        assert err
        assert 'Confirmation' in data['error']

    def test_rotation_confirmee_revoque_et_transfere(self, mcp_env):
        mcp = MCP(mcp_env.client, mcp_env.key_a)
        data, err = mcp.tool('rotate_api_key', {'confirm': True})
        assert not err, data
        new_key = data['api_key']
        assert new_key.startswith('sf_')
        assert data['credits_transferred'] == 10
        # l'ancienne clé est immédiatement révoquée
        r = mcp.raw({'jsonrpc': '2.0', 'id': 9, 'method': 'ping'})
        assert r.status_code == 401
        # la nouvelle fonctionne et porte le solde
        credits, _ = MCP(mcp_env.client, new_key).tool('get_credits')
        assert credits['credits_remaining'] == 10

    def test_achat_sans_confirmation_refuse(self, mcp_env):
        data, err = MCP(mcp_env.client, mcp_env.key_a).tool(
            'buy_credits', {'pack': 'starter'})
        assert err
        assert 'Confirmation' in data['error']

    def test_achat_pack_inconnu_refuse(self, mcp_env):
        data, err = MCP(mcp_env.client, mcp_env.key_a).tool(
            'buy_credits', {'pack': 'mega', 'confirm': True,
                            'customer_email': 'a@exemple.sn'})
        assert err

    def test_achat_cree_session_sans_prelever(self, mcp_env):
        mcp = MCP(mcp_env.client, mcp_env.key_a)
        data, err = mcp.tool('buy_credits', {
            'pack': 'starter', 'confirm': True,
            'customer_email': 'client@exemple.sn',
            'customer_name': 'Client Test'})
        assert not err, data
        assert data['amount_fcfa'] == 5000
        assert data['credits'] == 500
        assert data['status'] == 'pending'
        assert data['checkout_url']
        # aucun crédit tant que le paiement n'est pas confirmé
        credits, _ = mcp.tool('get_credits')
        assert credits['credits_remaining'] == 10
        # le paiement simulé (webhook signé) active les crédits — via
        # le MÊME chemin serveur que la production
        record = api_mod.billing_store.get(data['reference'])
        body, ts, sig, event = mcp_env.gateway.pay_session(
            record['session_id'], success=True)
        r = mcp_env.client.post(
            '/v1/billing/webhook', content=body, headers={
                'Content-Type': 'application/json',
                'X-Webhook-Signature': sig,
                'X-Webhook-Timestamp': ts,
                'X-Webhook-Event': event})
        assert r.status_code == 200
        credits, _ = mcp.tool('get_credits')
        assert credits['credits_remaining'] == 510


class TestSecuriteGlobale:
    def test_aucune_trace_de_secret_dans_les_reponses(self, mcp_env):
        """Les réponses MCP ne contiennent ni la clé appelante, ni les
        secrets internes (webhook secret, chemins des magasins)."""
        mcp = MCP(mcp_env.client, mcp_env.key_a)
        mcp.tool('convert_document', {
            'filename': 'r.csv', 'data_base64': _b64(WAVE_CSV)})
        for name, args in [('get_account_info', {}),
                           ('get_credits', {}),
                           ('get_limits', {}),
                           ('get_pricing', {}),
                           ('list_conversions', {}),
                           ('get_supported_formats', {})]:
            data, _ = mcp.tool(name, args)
            blob = json.dumps(data)
            assert mcp_env.key_a not in blob
            assert WEBHOOK_SECRET not in blob

    def test_rate_limit_par_cle(self, mcp_env, monkeypatch):
        limiter = mcp_http.RateLimiter(3, 60)
        monkeypatch.setattr(mcp_http, '_mcp_limiter', limiter)
        mcp = MCP(mcp_env.client, mcp_env.key_b)
        codes = [mcp.raw({'jsonrpc': '2.0', 'id': i, 'method': 'ping'})
                 .status_code for i in range(5)]
        assert codes.count(200) == 3
        assert codes.count(429) == 2
        # une autre clé n'est pas impactée
        r = MCP(mcp_env.client, mcp_env.key_a).raw(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        assert r.status_code == 200
