"""Tests de l'application web commerciale (v0.5.0) — exécutés contre
un VRAI PostgreSQL (pgserver embarqué ou SF_PG_TEST_URI externe) avec
les migrations RÉELLES du dépôt, migrations 005 comprises.

Couverture (phases 2/3/5/6/7 de la mission) :
- inscription (bonus crédits, clé API visible une fois, cookie session
  HttpOnly), connexion, déconnexion, session ;
- isolation stricte entre deux comptes (résultat, historique,
  rechargement) ;
- conversion RÉELLE depuis le tableau de bord : réservation → règlement,
  remboursement sur échec, crédits PostgreSQL ;
- rotation de clé (ancienne révoquée, solde transféré, nouvelle clé
  opère sur /v1/analyze) ;
- achat des 3 packs : montants SERVEUR, session de paiement, paiement
  simulé (FakeSasPay), webhook signé → activation, REJEU du webhook →
  aucun double crédit, statut polling sans webhook ;
- webhook à signature invalide → refus ;
- réinitialisation de mot de passe (token usage unique, sessions
  fermées) ;
- rate limiting (inscription, démo) ;
- tracking visites + statistiques admin (CA = UNIQUEMENT les
  rechargements activés — jamais les pending) ;
- frontend statique servi (landing, dashboard, manifest, sw, robots,
  échantillons) ;
- démo publique : moteur réel, sans compte, tronquée, rate-limitée ;
- vérification email par token (usage unique).

Ces tests sont IGNORÉS sans pgserver/psycopg (mode JSON pur).
"""
import base64
import os
import shutil

import pytest

import os as _os
if _os.environ.get('SF_PG_TEST_URI'):
    pytest.importorskip('psycopg')
    pytest.importorskip('psycopg_pool')
    pgserver = None
else:
    pgserver = pytest.importorskip('pgserver')
    pytest.importorskip('psycopg')
    pytest.importorskip('psycopg_pool')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATIONS = os.path.join(ROOT, 'supabase', 'migrations')
PGDATA_PARENT = '/tmp/sf-pgserver'
SOCKET_DIR = '/tmp/statementflow'


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
    shutil.rmtree(PGDATA_PARENT, ignore_errors=True)
    shutil.rmtree(SOCKET_DIR, ignore_errors=True)
    server = pgserver.get_server(PGDATA_PARENT)
    import atexit
    atexit.register(server.cleanup)
    return server


@pytest.fixture(scope='session')
def pg_db_uri(pg_server):
    import psycopg
    uri = pg_server.get_uri()
    if '?' in uri:
        path, _, query = uri.partition('?')
        db_uri = (path.rstrip('/').rsplit('/', 1)[0]
                  + '/sfwebapp?' + query)
    else:
        db_uri = uri.rstrip('/').rsplit('/', 1)[0] + '/sfwebapp'
    with psycopg.connect(uri, autocommit=True) as c:
        c.execute('drop database if exists sfwebapp')
        c.execute('create database sfwebapp')
    with psycopg.connect(db_uri, autocommit=True) as c:
        for name in sorted(os.listdir(MIGRATIONS)):
            if name.endswith('.sql'):
                with open(os.path.join(MIGRATIONS, name),
                          encoding='utf-8') as f:
                    c.execute(f.read())
    return db_uri


CSV_RELEVE = (b'Date,Heure,Type,Description,Montant,Solde\n'
              b'2026-03-02,10:15,ENVOI,Airtime purchase Orange SN,'
              b'-220871,554044\n'
              b'2026-03-03,10:15,ENVOI,Bill payment SENELEC 45210,'
              b'-33508,520536\n'
              b'2026-03-08,10:15,RECEPTION,Received from Mamadou Sow,'
              b'173555,694091\n')


@pytest.fixture()
def web_app(monkeypatch, pg_db_uri):
    """Application complète : PostgreSQL + FakeSasPay + PBKDF2 rapide
    (les itérations de prod sont testées ailleurs) + limiters vides."""
    import psycopg
    with psycopg.connect(pg_db_uri, autocommit=True) as c:
        c.execute('truncate organizations, users cascade')
    from statementflow import api as api_mod
    from statementflow import webapp as web_mod
    from statementflow.billing import FakeSasPay
    from statementflow.pg import PgStore, PgUsageStore
    store = PgStore(pg_db_uri, pool_min=0, pool_max=4)
    fake = FakeSasPay(webhook_secret='whsec-webapp')
    monkeypatch.setattr(api_mod, '_pg_store', store)
    monkeypatch.setattr(api_mod, 'managed_keys', store)
    monkeypatch.setattr(api_mod, 'billing_store', store)
    monkeypatch.setattr(api_mod, 'usage_store', PgUsageStore(store))
    monkeypatch.setattr(api_mod, 'saspay', fake)
    monkeypatch.setattr(web_mod, '_pg', lambda: store)
    monkeypatch.setenv('STATEMENTFLOW_SASPAY_WEBHOOK_SECRET',
                       'whsec-webapp')
    monkeypatch.setenv('STATEMENTFLOW_TEST_PAYMENT', '1')
    monkeypatch.setenv('STATEMENTFLOW_PBKDF2_ITERATIONS', '2000')
    monkeypatch.setenv('STATEMENTFLOW_SIGNUP_BONUS_CREDITS', '5')
    monkeypatch.setenv('STATEMENTFLOW_WEB_DIR',
                       os.path.join(ROOT, 'web'))
    api_mod._jobs.clear()
    # réinitialiser les limiters de débit (état module)
    for lim in (web_mod._signup_limiter, web_mod._login_limiter,
                web_mod._login_failures, web_mod._forgot_limiter,
                web_mod._track_limiter, web_mod._rotate_limiter,
                web_mod._resend_limiter, web_mod._demo_limiter):
        lim._events.clear()
    from fastapi.testclient import TestClient
    yield TestClient(api_mod.app), store, fake
    store.close()


def _signup(client, email='client@example.com', password='motdepasse123'):
    return client.post('/v1/auth/signup', json={
        'email': email, 'password': password,
        'anonymous_id': 'anon-1', 'ref': 'partenaire-x'})


def _login(client, email='client@example.com', password='motdepasse123'):
    return client.post('/v1/auth/login', json={
        'email': email, 'password': password})


class TestInscriptionConnexion:
    def test_signup_session_et_cle_api(self, web_app):
        client, store, _ = web_app
        r = _signup(client)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body['status'] == 'created'
        assert body['credits'] == 5
        assert body['api_key'].startswith('sf_')
        assert body['email_verification'] == 'not_configured'
        # session posée (sans SMTP : compte immédiatement utilisable)
        assert 'sf_session' in client.cookies
        s = client.get('/v1/auth/session').json()
        assert s['authenticated'] is True
        assert s['email'] == 'client@example.com'
        assert s['credits'] == 5
        # la clé API fonctionne sur l'API développeur
        r2 = client.post('/v1/analyze', files={'file': ('t.csv',
                                                         CSV_RELEVE)},
                         headers={'X-API-Key': body['api_key']})
        assert r2.status_code == 202
        # attribution marketing enregistrée
        stats = store.admin_stats()
        assert any(src['source'] == 'ref:partenaire-x'
                   for src in stats['signup_sources'])

    def test_signup_doublon_409_et_email_invalide(self, web_app):
        client, _, _ = web_app
        assert _signup(client).status_code == 201
        assert _signup(client).status_code == 409
        r = client.post('/v1/auth/signup',
                        json={'email': 'pas-un-email', 'password': 'x' * 12})
        assert r.status_code == 400

    def test_login_logout_et_mauvais_mot_de_passe(self, web_app):
        client, _, _ = web_app
        _signup(client, 'a@x.com', 'motdepasse123')
        client2 = client.__class__(client.app)   # jar vierge
        assert client2.get('/v1/auth/session'
                           ).json()['authenticated'] is False
        r = _login(client2, 'a@x.com', 'mauvais-mot-de-passe')
        assert r.status_code == 401
        r = _login(client2, 'a@x.com', 'motdepasse123')
        assert r.status_code == 200
        assert client2.get('/v1/auth/session'
                           ).json()['authenticated'] is True
        assert client2.post('/v1/auth/logout').status_code == 200
        assert client2.get('/v1/auth/session'
                           ).json()['authenticated'] is False

    def test_rate_limit_inscription(self, web_app):
        client, _, _ = web_app
        for i in range(5):
            assert _signup(client, f'u{i}@x.com').status_code == 201
        assert _signup(client, 'u5@x.com').status_code == 429

    def test_reset_mot_de_passe_token_usage_unique(self, web_app):
        client, store, _ = web_app
        _signup(client, 'reset@x.com', 'motdepasse123')
        from statementflow.accounts import hash_token, new_token
        user = store.get_user_by_email('reset@x.com')
        token, token_hash = new_token()
        store.create_auth_token(user['id'], 'password_reset', token_hash,
                                3600)
        c2 = client.__class__(client.app)
        r = c2.post('/v1/auth/password/reset',
                    json={'token': token, 'password': 'nouveaumdp123'})
        assert r.status_code == 200
        # le token consommé ne marche plus
        r = c2.post('/v1/auth/password/reset',
                    json={'token': token, 'password': 'encoreunmdp123'})
        assert r.status_code == 400
        # l'ancien mot de passe est refusé, le nouveau accepté
        assert _login(c2, 'reset@x.com', 'motdepasse123').status_code == 401
        assert _login(c2, 'reset@x.com', 'nouveaumdp123').status_code == 200


class TestConversionDashboard:
    def test_conversion_reelle_et_facturation(self, web_app):
        client, store, _ = web_app
        _signup(client, 'conv@x.com')
        r = client.post('/v1/account/convert',
                        files={'file': ('releve.csv', CSV_RELEVE)})
        assert r.status_code == 202, r.text
        job = r.json()['job_id']
        r2 = client.get(f'/v1/account/result/{job}')
        assert r2.status_code == 200
        body = r2.json()
        assert body['status'] == 'done'
        assert len(body['transactions']) == 3
        assert body['billing']['billing_mode'] == 'managed'
        assert body['billing']['credits_charged'] == 1
        assert body['billing']['credits_remaining'] == 4
        # solde réellement débité dans PostgreSQL
        user = store.get_user_by_email('conv@x.com')
        kh = store.user_active_key_hash(user['id'])
        assert store.balance(kh) == 4
        # historique visible
        hist = client.get('/v1/account/conversions').json()['records']
        assert hist and hist[0]['status'] == 'done'
        assert hist[0]['credits_charged'] == 1
        # JSON téléchargeable : le résultat complet est renvoyé
        assert 'checks' in body

    def test_conversion_echouee_remoursement(self, web_app):
        client, store, _ = web_app
        _signup(client, 'echec@x.com')
        r = client.post('/v1/account/convert',
                        files={'file': ('x.pdf', b'%PDF-casse')})
        assert r.status_code == 202
        job = r.json()['job_id']
        r2 = client.get(f'/v1/account/result/{job}')
        assert r2.status_code == 422
        user = store.get_user_by_email('echec@x.com')
        kh = store.user_active_key_hash(user['id'])
        assert store.balance(kh) == 5   # rien n'est débité

    def test_isolation_entre_comptes(self, web_app):
        client_a, store, _ = web_app
        client_b = client_a.__class__(client_a.app)
        _signup(client_a, 'a@x.com')
        _signup(client_b, 'b@x.com')
        job = client_a.post('/v1/account/convert',
                            files={'file': ('r.csv', CSV_RELEVE)}
                            ).json()['job_id']
        # B ne voit PAS le résultat de A
        assert client_b.get(f'/v1/account/result/{job}'
                            ).status_code == 404
        # B ne voit PAS l'historique de A
        recs_b = client_b.get('/v1/account/conversions'
                              ).json()['records']
        assert recs_b == []
        # les crédits de A n'ont pas bougé pour B
        assert client_b.get('/v1/auth/session').json()['credits'] == 5
        assert client_a.get('/v1/auth/session').json()['credits'] == 4
        # sans session : 401
        anon = client_a.__class__(client_a.app)
        assert anon.get('/v1/account/summary').status_code == 401
        assert anon.post('/v1/account/convert',
                         files={'file': ('r.csv', CSV_RELEVE)}
                         ).status_code == 401

    def test_rotation_cle_solde_transfere(self, web_app):
        client, store, _ = web_app
        key = _signup(client, 'rot@x.com').json()['api_key']
        # consomme 1 crédit via l'API développeur
        client.post('/v1/analyze', files={'file': ('t.csv', CSV_RELEVE)},
                    headers={'X-API-Key': key})
        r = client.post('/v1/account/key/rotate',
                        json={'confirm': True})
        assert r.status_code == 200
        body = r.json()
        assert body['credits_transferred'] == 4
        new_key = body['api_key']
        # l'ancienne clé est révoquée
        assert client.post('/v1/analyze',
                           files={'file': ('t.csv', CSV_RELEVE)},
                           headers={'X-API-Key': key}).status_code == 401
        # la nouvelle fonctionne et porte le solde
        r2 = client.post('/v1/analyze',
                         files={'file': ('t.csv', CSV_RELEVE)},
                         headers={'X-API-Key': new_key})
        assert r2.status_code == 202
        job = r2.json()['job_id']
        res = client.get(f'/v1/results/{job}',
                         headers={'X-API-Key': new_key}).json()
        assert res['billing']['credits_remaining'] == 3
        # sans confirmation explicite : refus
        assert client.post('/v1/account/key/rotate',
                           json={'confirm': False}).status_code == 400


class TestAchatsSasPay:
    def test_trois_packs_montants_serveur(self, web_app):
        client, store, fake = web_app
        _signup(client, 'acheteur@x.com')
        attendus = {'starter': (500, 5000), 'pro': (2500, 20000),
                    'business': (10000, 60000)}
        refs = {}
        for pack, (credits, montant) in attendus.items():
            r = client.post('/v1/account/topup', json={'pack': pack})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body['credits'] == credits
            assert body['amount_fcfa'] == montant
            assert body['checkout_url'].startswith('https://')
            refs[pack] = body['reference']
        # pack inconnu refusé ; montant non trusté du navigateur
        assert client.post('/v1/account/topup',
                           json={'pack': 'gratuit'}).status_code == 400
        # statut pending avant paiement
        st = client.get(f"/v1/account/topup/{refs['starter']}").json()
        assert st['status'] == 'pending'

    def _payer_via_webhook(self, client, fake, reference):
        record = None
        # retrouver la session SasPay du rechargement
        for sid, sess in fake._sessions.items():
            if sess['metadata'].get('pack'):
                pass
        # via le statut : le record contient session_id
        store = None
        # plus simple : itérer sur les topups du compte
        topups = client.get('/v1/account/topups').json()['records']
        ref = next(t for t in topups if t['reference'] == reference)
        # session_id : via le store
        return ref

    def test_cycle_paiement_webhook_et_idempotence(self, web_app):
        client, store, fake = web_app
        _signup(client, 'pay@x.com')
        ref = client.post('/v1/account/topup',
                          json={'pack': 'pro'}).json()['reference']
        record = store.get(ref)
        # paiement simulé → webhook signé
        webhook = fake.pay_session(record['session_id'], success=True)
        assert webhook is not None
        raw, ts, sig, event = webhook
        r = client.post('/v1/billing/webhook', content=raw, headers={
            'X-Webhook-Signature': sig,
            'X-Webhook-Timestamp': str(ts),
            'X-Webhook-Event': event})
        assert r.status_code == 200, r.text
        # crédits activés immédiatement
        s = client.get('/v1/auth/session').json()
        assert s['credits'] == 5 + 2500
        # REJOUER le webhook : aucun double crédit
        r2 = client.post('/v1/billing/webhook', content=raw, headers={
            'X-Webhook-Signature': sig,
            'X-Webhook-Timestamp': str(ts),
            'X-Webhook-Event': event})
        assert r2.status_code == 200
        assert client.get('/v1/auth/session'
                          ).json()['credits'] == 5 + 2500

    def test_webhook_signature_invalide_refusee(self, web_app):
        client, store, fake = web_app
        _signup(client, 'secu@x.com')
        ref = client.post('/v1/account/topup',
                          json={'pack': 'starter'}).json()['reference']
        record = store.get(ref)
        webhook = fake.pay_session(record['session_id'])
        raw, ts, sig, event = webhook
        bad = client.post('/v1/billing/webhook', content=raw, headers={
            'X-Webhook-Signature': 'deadbeef' * 8,
            'X-Webhook-Timestamp': str(ts),
            'X-Webhook-Event': event})
        assert bad.status_code == 401
        # aucun crédit activé
        assert client.get('/v1/auth/session'
                          ).json()['credits'] == 5

    def test_polling_statut_sans_webhook(self, web_app):
        """Retour navigateur SANS webhook : le polling du statut active
        les crédits (le webhook manquant est rattrapé)."""
        client, store, fake = web_app
        _signup(client, 'poll@x.com')
        ref = client.post('/v1/account/topup',
                          json={'pack': 'business'}).json()['reference']
        record = store.get(ref)
        fake.pay_session(record['session_id'])  # payé, webhook IGNORÉ
        st = client.get(f'/v1/account/topup/{ref}').json()
        assert st['status'] == 'activated'
        assert st['credits'] == 10000
        assert st['balance'] == 5 + 10000

    def test_paiement_echoue_marque_failed(self, web_app):
        client, store, fake = web_app
        _signup(client, 'fail@x.com')
        ref = client.post('/v1/account/topup',
                          json={'pack': 'starter'}).json()['reference']
        record = store.get(ref)
        fake.pay_session(record['session_id'], success=False)
        st = client.get(f'/v1/account/topup/{ref}').json()
        assert st['status'] == 'failed'
        assert client.get('/v1/auth/session').json()['credits'] == 5

    def test_isolation_rechargements(self, web_app):
        client_a, store, fake = web_app
        client_b = client_a.__class__(client_a.app)
        _signup(client_a, 'iso-pay@x.com')
        _signup(client_b, 'iso-pay-b@x.com')
        ref = client_a.post('/v1/account/topup',
                            json={'pack': 'starter'}).json()['reference']
        assert client_b.get(f'/v1/account/topup/{ref}'
                            ).status_code == 404


class TestTrackingEtStats:
    def test_visites_et_stats_admin(self, web_app):
        client, store, fake = web_app
        # 2 visiteurs anonymes
        for aid in ('anon-a', 'anon-b'):
            r = client.post('/v1/track', json={
                'type': 'visit', 'path': '/', 'anonymous_id': aid,
                'referrer': 'https://www.google.com/'})
            assert r.status_code == 200
        # inscription + conversion + achat activé
        _signup(client, 'stats@x.com')
        client.post('/v1/account/convert',
                    files={'file': ('r.csv', CSV_RELEVE)})
        ref = client.post('/v1/account/topup',
                          json={'pack': 'starter'}).json()['reference']
        record = store.get(ref)
        fake.pay_session(record['session_id'])
        # le client revient : le polling active les crédits
        st = client.get(f"/v1/account/topup/{ref}").json()
        assert st['status'] == 'activated'
        # un second rechargement NON payé (pending → hors CA)
        client.post('/v1/account/topup', json={'pack': 'pro'})
        # stats admin : clé requise
        assert client.get('/v1/admin/stats').status_code == 403
        assert client.get('/v1/admin/stats',
                          headers={'X-Admin-Key': 'nimporte'
                                   }).status_code == 403
        from statementflow.security import hash_key
        admin_key = 'sf-admin-key-test'
        from statementflow import api as api_mod
        original = api_mod.admin_auth
        from statementflow.security import AdminAuthorizer
        api_mod.admin_auth = AdminAuthorizer(hash_key(admin_key))
        try:
            r = client.get('/v1/admin/stats',
                           headers={'X-Admin-Key': admin_key})
            assert r.status_code == 200, r.text
            s = r.json()
        finally:
            api_mod.admin_auth = original
        assert s['visitors'] == 2
        assert s['signups'] == 1
        assert s['active_users'] == 1
        assert s['conversions_done'] == 1
        assert s['credits_consumed'] == 1
        # CA : UNIQUEMENT le starter activé (5 000) — le pro pending
        # ne compte JAMAIS
        assert s['revenue_fcfa'] == 5000
        assert s['payments_activated'] == 1
        assert s['packs'][0]['pack'] == 'starter'
        assert s['packs'][0]['count'] == 1
        assert any(src['source'] == 'ref:partenaire-x'
                   for src in s['signup_sources'])

    def test_rate_limit_tracking(self, web_app):
        client, _, _ = web_app
        for _ in range(120):
            client.post('/v1/track', json={
                'type': 'visit', 'anonymous_id': 'spam'})
        assert client.post('/v1/track', json={
            'type': 'visit', 'anonymous_id': 'spam'}).status_code == 429


class TestVerificationEmail:
    def test_token_verification_usage_unique(self, web_app):
        client, store, _ = web_app
        # SMTP non configuré : signup → auto_no_smtp (vérifié auto)
        _signup(client, 'verif@x.com')
        user = store.get_user_by_email('verif@x.com')
        assert user['email_verified'] is True
        assert user['verified_via'] == 'auto_no_smtp'
        # un token de vérification consommé ne marche pas deux fois
        from statementflow.accounts import new_token
        token, th = new_token()
        store.create_auth_token(user['id'], 'verify_email', th, 3600)
        store._call(lambda c: c.execute(
            "update users set email_verified = false,"
            " verified_via = null where id = %s::uuid", (user['id'],)))
        r = client.post('/v1/auth/verify', json={'token': token})
        assert r.status_code == 200
        assert client.post('/v1/auth/verify',
                           json={'token': token}).status_code == 400


class TestDemoPublique:
    def test_demo_sans_compte_moteur_reel(self, web_app):
        client, _, _ = web_app
        r = client.post('/v1/demo/analyze',
                        files={'file': ('r.csv', CSV_RELEVE)})
        assert r.status_code == 200, r.text
        body = r.json()
        assert 'Date,Heure' in body['input_preview']
        assert len(body['result']['transactions']) == 3
        assert body['result']['billing']['credits_equivalent'] == 1
        # aucun compte créé, aucun crédit débité (rien à vérifier :
        # la démo ne touche pas au grand livre)
        assert client.get('/v1/auth/session'
                          ).json()['authenticated'] is False

    def test_demo_rate_limit_et_type_fichier(self, web_app):
        client, _, _ = web_app
        # fichier invalide → 422 explicite (AVANT de saturer le limiter)
        assert client.post('/v1/demo/analyze',
                           files={'file': ('x.pdf', b'%PDF-casse')}
                           ).status_code == 422
        for _ in range(5):
            assert client.post('/v1/demo/analyze',
                               files={'file': ('r.csv', CSV_RELEVE)}
                               ).status_code == 200
        # 6e fichier → 429 (limite : 6/heure, 1 déjà consommé + 5)
        assert client.post('/v1/demo/analyze',
                           files={'file': ('r.csv', CSV_RELEVE)}
                           ).status_code == 429


class TestFrontendStatique:
    def test_pages_et_assets_servis(self, web_app):
        client, _, _ = web_app
        pages = ['/', '/index.html', '/demo.html', '/signup.html',
                 '/login.html', '/dashboard.html', '/admin.html',
                 '/legal.html', '/api.html', '/forgot.html',
                 '/reset.html', '/offline.html', '/style.css',
                 '/app.js', '/sw.js', '/manifest.webmanifest',
                 '/robots.txt', '/sitemap.xml', '/favicon.svg',
                 '/icon-192.png', '/samples/releve-mobile-money.csv',
                 '/samples/exemple-resultat.json']
        for path in pages:
            r = client.get(path)
            assert r.status_code == 200, f'{path} → {r.status_code}'
            assert len(r.content) > 50
        # la landing contient le SEO attendu
        home = client.get('/').text
        for marker in ('og:title', 'sitemap.xml', 'StatementFlow',
                       '5 000 FCFA', 'FAQ', 'manifest.webmanifest'):
            assert marker in home, marker
        # les routes API restent prioritaires sur le statique
        assert client.get('/health').status_code == 200
        assert client.get('/v1/billing/packs').status_code == 200

    def test_api_doc_disponible(self, web_app):
        client, _, _ = web_app
        assert client.get('/docs').status_code == 200
        spec = client.get('/openapi.json').json()
        for route in ('/v1/auth/signup', '/v1/account/convert',
                      '/v1/account/topup', '/v1/demo/analyze',
                      '/v1/track'):
            assert route in spec['paths'], route
