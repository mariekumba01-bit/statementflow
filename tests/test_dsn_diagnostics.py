"""Tests du diagnostic et de la sanitisation DSN PostgreSQL (v0.4.1).

Incident Render réel (2026-11-08) : un caractère U+200E (LEFT-TO-RIGHT
MARK, catégorie Unicode Cf) copié-collé devant STATEMENTFLOW_DATABASE_URL
faisait échouer libpq avec « missing "=" after "postgresql://…" in
connection info string » — str.strip() ne retire PAS ces caractères
(isspace() == False). Le message libpq contenait en outre la chaîne
COMPLÈTE avec le mot de passe → fuite de secret dans les logs.

Couverture :
- sanitize_dsn : marques bidi (U+200E/U+200F), BOM, zero-width, NBSP,
  espaces, Cc (contrôles) ; préservation du reste à l'identique ;
- _validate_dsn : PgStoreError SANS le DSN quand libpq refuse le
  format (le message psycopg brut contient le mot de passe) ;
- classify_pg_error : dsn_invalid / auth_failed / unreachable / tls ;
- e2e : PgStore avec un DSN pollué par U+200E → ping() réussit sur un
  vrai PostgreSQL (la sanitisation rend la connexion FONCTIONNELLE,
  pas seulement silencieuse) ;
- persistance : écriture → fermeture du pool → réouverture → lecture ;
- no-leak : aucun message d'erreur ne contient le mot de passe ;
- pooler Supabase 6543 : prepare_threshold=None bien appliqué ;
- coexistence hash-list + clés gérées (api.py) — clé gérée acceptée.
"""
import os
import shutil

import pytest

pgserver = pytest.importorskip('pgserver')
pytest.importorskip('psycopg')
pytest.importorskip('psycopg_pool')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATIONS = os.path.join(ROOT, 'supabase', 'migrations')
PGDATA_PARENT = '/tmp/sf-pgserver'
SOCKET_DIR = '/tmp/statementflow'

PASSWORD = 'mot-de-passe-test'  # factice — sert de canari de fuite


@pytest.fixture(scope='session')
def pg_server():
    if os.environ.get('SF_PG_TEST_URI'):
        class _Ext:
            def get_uri(self):
                return os.environ['SF_PG_TEST_URI']

            def cleanup(self):
                pass
        return _Ext()
    shutil.rmtree(PGDATA_PARENT, ignore_errors=True)
    shutil.rmtree(SOCKET_DIR, ignore_errors=True)
    server = pgserver.get_server(PGDATA_PARENT)
    import atexit
    atexit.register(server.cleanup)
    return server


def _db_uri(base_uri: str, dbname: str) -> str:
    if '?' in base_uri:
        path, _, query = base_uri.partition('?')
        return path.rstrip('/').rsplit('/', 1)[0] + '/' + dbname + '?' + query
    return base_uri.rstrip('/').rsplit('/', 1)[0] + '/' + dbname


@pytest.fixture(scope='session')
def pg_db_uri(pg_server):
    """Base migrée ; le rôle porte un MOT DE PASSE (comme Supabase) :
    tout message d'erreur qui le contiendrait est détecté par les tests
    no-leak."""
    import psycopg
    uri = pg_server.get_uri()
    with psycopg.connect(uri, autocommit=True) as c:
        has_role = c.execute(
            "select 1 from pg_roles where rolname = 'sfuser'").fetchone()
        if has_role is None:
            c.execute(f"create role sfuser login password '{PASSWORD}'")
        c.execute('drop database if exists sfdsn')
        c.execute('create database sfdsn owner sfuser')
    # URI AVEC mot de passe (format Supabase réel) : host=socket passé
    # en query param (format pgserver), conservé tel quel — les tables
    # doivent appartenir au rôle QUI LES UTILISE (sfuser), comme sur
    # Supabase où le rôle de la chaîne de connexion est propriétaire
    params = psycopg.conninfo.conninfo_to_dict(uri)
    port = params.get('port') or 5432
    if '?' in uri:
        query = uri.partition('?')[2]
        sf_uri = (f'postgresql://sfuser:{PASSWORD}@localhost:{port}'
                  f'/sfdsn?{query}')
    else:
        host = params.get('host') or 'localhost'
        sf_uri = f'postgresql://sfuser:{PASSWORD}@{host}:{port}/sfdsn'
    with psycopg.connect(sf_uri, autocommit=True) as c:
        for name in sorted(os.listdir(MIGRATIONS)):
            if name.endswith('.sql'):
                with open(os.path.join(MIGRATIONS, name),
                          encoding='utf-8') as f:
                    c.execute(f.read())
    return sf_uri


class TestSanitizeDsn:
    def test_ltr_mark_u200e_retiree(self):
        from statementflow.pg import sanitize_dsn
        polluted = '\u200epostgresql://u:p@h:5432/db'
        assert sanitize_dsn(polluted) == 'postgresql://u:p@h:5432/db'

    def test_rtl_mark_et_bom_et_zero_width(self):
        from statementflow.pg import sanitize_dsn
        assert sanitize_dsn('\u200fpostgresql://x') == 'postgresql://x'
        assert sanitize_dsn('\ufeffpostgresql://x') == 'postgresql://x'
        assert sanitize_dsn('post\u200bgresql://x') == 'postgresql://x'
        assert sanitize_dsn('postgresql://x\u200d') == 'postgresql://x'

    def test_nbsp_et_espaces(self):
        from statementflow.pg import sanitize_dsn
        assert sanitize_dsn('\u00a0postgresql://x ') == 'postgresql://x'
        assert sanitize_dsn('  postgresql://x\t') == 'postgresql://x'

    def test_controles_cc(self):
        from statementflow.pg import sanitize_dsn
        assert sanitize_dsn('\x00\x01postgresql://x') == 'postgresql://x'

    def test_chaines_valides_inchangees(self):
        from statementflow.pg import sanitize_dsn
        ok = 'postgresql://postgres.ABCDEF:pa%40ss@aws-0-eu-central-1.pooler.supabase.com:6543/postgres'
        assert sanitize_dsn(ok) == ok
        assert sanitize_dsn('') == ''

    def test_mot_de_passe_avec_espace_interne_preserve(self):
        from statementflow.pg import sanitize_dsn
        dsn = 'postgresql://u:my pass@h/db'
        assert sanitize_dsn(dsn) == dsn  # l'intérieur n'est pas touché


class TestValidateDsnNoLeak:
    def test_dsn_invalide_erreur_sans_secret(self):
        """DSN invalide → ping() False + last_error_kind='dsn_invalid'.
        Le message ACTIONNABLE (avec le nom de la variable) est porté
        par la PgStoreError — vérifiée aussi directement."""
        from statementflow.pg import PgStore, PgStoreError
        store = PgStore('pas-un-dsn-valide-du-tout')
        assert store.ping() is False
        assert store.last_error_kind == 'dsn_invalid'
        # l'erreur directe est actionnable et sans secret
        with pytest.raises(PgStoreError) as ei:
            store._validate_dsn()
        msg = str(ei.value)
        assert 'DSN PostgreSQL invalide' in msg
        assert 'STATEMENTFLOW_DATABASE_URL' in msg
        assert 'pas-un-dsn' not in msg  # le DSN lui-même ne fuite pas

    def test_uri_tronquee_erreur_sans_secret(self):
        from statementflow.pg import PgStore, PgStoreError
        # URI valide en apparence mais hôte inexistant : l'erreur est
        # classifiée, le message psycopg (qui peut contenir la chaîne)
        # ne remonte jamais
        store = PgStore('postgresql://user:le-mot@invalide.example:5432/db',
                        pool_timeout=2)
        assert store.ping() is False
        assert store.last_error_kind in ('unreachable', 'auth_failed',
                                         'unknown')
        assert store.last_error_kind  # toujours classifié, jamais None

    def test_aucun_message_ne_contient_le_mot_de_passe(self):
        """Les DEUX chemins d'échec (DSN invalide, hôte injoignable)
        produisent des messages sans le secret. Les messages libpq
        BRUTS contiennent la chaîne complète — ils ne doivent jamais
        fuiter via PgStoreError, ping(), /health ni le cron."""
        from statementflow.pg import PgStore, PgStoreError
        secret = 'canari-super-secret'
        # chemin 1 : U+200E retiré → URI valide → hôte injoignable
        store = PgStore(
            f'\u200epostgresql://u:{secret}@hote-invalide-xyz:5432/db',
            pool_timeout=2)
        store._validate_dsn()  # la sanitisation rend le parsing OK
        assert store.ping() is False
        assert secret not in (store.last_error_kind or '')
        # chemin 2 : DSN syntaxiquement invalide (espaces internes)
        store2 = PgStore(f'x\u200ey z:{secret}\u200f', pool_timeout=2)
        assert store2.ping() is False
        assert store2.last_error_kind == 'dsn_invalid'
        with pytest.raises(PgStoreError) as ei:
            store2._validate_dsn()
        assert secret not in str(ei.value)


class TestClassifyPgError:
    def test_classification(self):
        from statementflow.pg import classify_pg_error

        class E(Exception):
            def __init__(self, msg):
                super().__init__(msg)

        assert classify_pg_error(E(
            'missing "=" after "postgresql://x" in connection info string'
        )) == 'dsn_invalid'
        assert classify_pg_error(E(
            'connection to server at "h" failed: password authentication '
            'failed for user "u"')) == 'auth_failed'
        assert classify_pg_error(E(
            'connection to server at "h" (10.0.0.1) port 5432 failed: '
            'Connection refused')) == 'unreachable'
        assert classify_pg_error(E(
            'connection failed: ssl certificate mismatch')) == 'tls_error'
        assert classify_pg_error(E('autre chose')) == 'unknown'


class TestEndToEndSanitizedConnection:
    """La sanitisation rend la connexion RÉELLEMENT fonctionnelle."""

    def test_ping_avec_u200e(self, pg_db_uri):
        from statementflow.pg import PgStore
        store = PgStore('\u200e' + pg_db_uri + '\u200f',
                        pool_min=0, pool_max=2)
        try:
            assert store.ping() is True
            assert store.last_error_kind is None
        finally:
            store.close()

    def test_ecriture_lecture_persistance(self, pg_db_uri):
        """Écriture → fermeture du pool → NOUVEAU store (nouveau pool)
        → la donnée est toujours là (persistance réelle, pas mémoire)."""
        from statementflow.pg import PgStore
        import psycopg
        with psycopg.connect(pg_db_uri, autocommit=True) as c:
            c.execute('truncate organizations cascade')

        s1 = PgStore('\ufeff' + pg_db_uri, pool_min=0, pool_max=2)
        key, rec = s1.issue('persiste-test', credits=7)
        kh = rec['key_hash']
        assert s1.balance(kh) == 7
        s1.close()                     # le pool disparaît

        s2 = PgStore(pg_db_uri + ' ', pool_min=0, pool_max=2)
        try:
            assert s2.balance(kh) == 7         # PERSISTÉ en base
            assert s2.lookup(kh)['label'] == 'persiste-test'
            # opérations financières sur le store réouvert
            assert s2.reserve(kh, 1.0, 'job-persist-1') is True
            assert s2.balance(kh) == 6
            assert s2.settle(kh, 2.0, 'job-persist-1') == 4
        finally:
            s2.close()

    def test_pooler_6543_prepare_threshold(self):
        """Le pooler Supabase en mode transaction (port 6543) interdit
        les prepared statements persistants : psycopg3 doit les avoir
        désactivés (prepare_threshold=None)."""
        from statementflow.pg import PgStore
        store = PgStore('postgresql://u:p@h:6543/db')
        assert store._pool_kwargs({'port': '6543'}) == {
            'prepare_threshold': None}
        assert store._pool_kwargs({'port': '5432'}) == {}
        assert store._pool_kwargs({}) == {}
        # et via le parseur libpq (port en chaîne, comme libpq)
        params = store._validate_dsn()
        assert str(params.get('port')) == '6543'


class TestCoexistenceCles:
    """v0.4.1 : avec hash-list ET magasin géré (PostgreSQL), une clé
    gérée (client facturé) doit être acceptée — sinon les opérations
    financières via la base seraient impossibles (incident Render :
    auth_mode key-hash-list bloquait les clés gérées)."""

    def test_cle_geree_acceptee_avec_hash_list(self, pg_db_uri,
                                               monkeypatch):
        import psycopg
        with psycopg.connect(pg_db_uri, autocommit=True) as c:
            c.execute('truncate organizations cascade')
        from statementflow import api as api_mod
        from statementflow.pg import PgStore, PgUsageStore
        from statementflow.billing import FakeSasPay
        from statementflow.security import ApiKeyStore, hash_key
        store = PgStore(pg_db_uri, pool_min=0, pool_max=2)
        key, rec = store.issue('coexist-client', credits=5)
        technical_key = 'sf-cle-technique-test'
        monkeypatch.setattr(api_mod, 'managed_keys', store)
        monkeypatch.setattr(api_mod, 'billing_store', store)
        monkeypatch.setattr(api_mod, 'usage_store', PgUsageStore(store))
        monkeypatch.setattr(api_mod, 'api_keys', ApiKeyStore(
            [hash_key(technical_key)]))  # hash-list (clé technique)
        monkeypatch.setattr(api_mod, 'saspay',
                            FakeSasPay(webhook_secret='whsec-x'))
        api_mod._jobs.clear()
        from fastapi.testclient import TestClient
        client = TestClient(api_mod.app)
        try:
            # la clé gérée PostgreSQL passe l'authentification
            csv = (b'Date;Libelle;Debit;Credit;Solde\n'
                   b'01/02/2026;A;100;;900\n')
            r = client.post('/v1/analyze',
                            files={'file': ('t.csv', csv)},
                            headers={'X-API-Key': key})
            assert r.status_code == 202, f'{r.status_code}: {r.text[:300]}'
            job = r.json()['job_id']
            r2 = client.get(f'/v1/results/{job}',
                            headers={'X-API-Key': key})
            assert r2.status_code == 200
            billing = r2.json()['billing']
            assert billing['billing_mode'] == 'managed'
            assert billing['credits_charged'] == 1
            assert billing['credits_remaining'] == 4
            # le solde a BIEN bougé dans PostgreSQL
            assert store.balance(rec['key_hash']) == 4
            # la clé technique hash-list (inconnue de la base) passe
            # aussi — non facturée
            r3 = client.post('/v1/analyze',
                             files={'file': ('t.csv', csv)},
                             headers={'X-API-Key': technical_key})
            assert r3.status_code == 202
            r4 = client.get(f"/v1/results/{r3.json()['job_id']}",
                            headers={'X-API-Key': technical_key})
            assert r4.status_code == 200
            assert r4.json()['billing']['billing_mode'] == 'unmanaged'
            # une clé inconnue des DEUX registres reste refusée
            r5 = client.post('/v1/analyze',
                             files={'file': ('t.csv', csv)},
                             headers={'X-API-Key': 'inconnue-total'})
            assert r5.status_code == 401
        finally:
            store.close()


class TestMaintenanceDiagnostic:
    def test_maintenance_avec_dsn_pourri_sortie_3(self, capsys):
        """DSN invalide → code 3 (erreur technique), message sans le
        DSN ni le mot de passe."""
        import os
        from statementflow import maintenance
        old = os.environ.get('STATEMENTFLOW_DATABASE_URL')
        os.environ['STATEMENTFLOW_DATABASE_URL'] = \
            'garbage-pas-une-uri@#!'
        try:
            rc = maintenance.main([])
        finally:
            if old is None:
                os.environ.pop('STATEMENTFLOW_DATABASE_URL', None)
            else:
                os.environ['STATEMENTFLOW_DATABASE_URL'] = old
        assert rc == 3
        err = capsys.readouterr().err
        assert 'garbage' not in err  # le DSN ne fuite pas
        assert 'PostgreSQL indisponible' in err
