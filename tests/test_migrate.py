"""Tests du moteur d'auto-migration contrôlée (v0.5.1) — vraie base
PostgreSQL (pgserver embarqué ou SF_PG_TEST_URI externe), migrations
réelles du dépôt.

Couverture :
- application sur base vierge : les 5 fichiers tracés, objets créés ;
- RE-JEU intégral : idempotent (aucune erreur, rien de nouveau) ;
- tracker : un fichier appliqué n'est pas réappliqué ;
- checksum : un fichier déjà appliqué puis MODIFIÉ est refusé ;
- contrat additif : un fichier contenant « drop table » / « truncate »
  est REFUSÉ avant exécution (et la base reste intacte) ;
- résolution du répertoire (env → CWD → paquet) ;
- run_for_store : désactivable (AUTO_MIGRATE=0), échec non bloquant.

Ignoré sans pgserver/psycopg.
"""
import os
import shutil

import pytest

import os as _os
if _os.environ.get('SF_PG_TEST_URI'):
    pytest.importorskip('psycopg')
    pgserver = None
else:
    pgserver = pytest.importorskip('pgserver')
    pytest.importorskip('psycopg')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATIONS = os.path.join(ROOT, 'supabase', 'migrations')
sys_path_root = ROOT
PGDATA = '/tmp/sf-pgserver-migrate'
SOCKET_DIR = '/tmp/statementflow'


class _External:
    def __init__(self, uri):
        self._uri = uri

    def get_uri(self):
        return self._uri

    def cleanup(self):
        pass


@pytest.fixture(scope='module')
def server():
    if os.environ.get('SF_PG_TEST_URI'):
        return _External(os.environ['SF_PG_TEST_URI'])
    shutil.rmtree(PGDATA, ignore_errors=True)
    shutil.rmtree(SOCKET_DIR, ignore_errors=True)
    srv = pgserver.get_server(PGDATA)
    import atexit
    atexit.register(srv.cleanup)
    return srv


def _fresh_db(server, name):
    import psycopg
    uri = server.get_uri()
    with psycopg.connect(uri, autocommit=True) as c:
        c.execute(f'drop database if exists {name}')
        c.execute(f'create database {name}')
    q = uri.partition('?')[2]
    if q:
        return f'postgresql://postgres:@localhost:5432/{name}?{q}'
    return uri.rsplit('/', 1)[0] + f'/{name}'


def _objects(dsn):
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as c:
        tables = {r[0] for r in c.execute(
            "select tablename from pg_tables"
            " where schemaname='public'").fetchall()}
        funcs = {r[0] for r in c.execute(
            "select proname from pg_proc p"
            " join pg_namespace n on n.oid=p.pronamespace"
            " where n.nspname='public'").fetchall()}
        tracked = {r[0] for r in c.execute(
            'select name from schema_migrations').fetchall()}
        rows = {t: c.execute(f'select count(*) from {t}').fetchone()[0]
                for t in ('organizations', 'users', 'api_keys',
                          'credit_ledger')}
        return tables, funcs, tracked, rows


class TestAutoMigration:
    def test_application_vierge_puis_rejeu(self, server):
        from statementflow.migrate import run_migrations
        dsn = _fresh_db(server, 'sfmigrate1')
        # 1. base vierge : les 5 fichiers sont appliqués et tracés
        applied = run_migrations(dsn, directory=MIGRATIONS, verbose=False)
        assert len(applied) == 5
        tables, funcs, tracked, rows = _objects(dsn)
        assert {'users', 'sessions', 'auth_tokens', 'events',
                'schema_migrations'} <= tables
        assert {'create_account', 'rotate_user_key', 'reserve_credits',
                'settle_credits', 'activate_topup'} <= funcs
        assert len(tracked) == 5
        assert all(n == 0 for n in rows.values())  # rien d'écrit
        # 2. re-jeu intégral : rien de nouveau, aucune erreur
        applied2 = run_migrations(dsn, directory=MIGRATIONS, verbose=False)
        assert applied2 == []
        t2, f2, tr2, r2 = _objects(dsn)
        assert t2 == tables and f2 == funcs and tr2 == tracked
        assert r2 == rows

    def test_fichier_deja_applique_modifie_refuse(self, server, tmp_path):
        from statementflow.migrate import MigrationError, run_migrations
        dsn = _fresh_db(server, 'sfmigrate2')
        run_migrations(dsn, directory=MIGRATIONS, verbose=False)
        # copie des migrations avec un fichier APPLIÉ modifié
        mod_dir = tmp_path / 'migrations'
        shutil.copytree(MIGRATIONS, mod_dir)
        target = mod_dir / '20261109000005_comptes_clients.sql'
        target.write_text(target.read_text(encoding='utf-8')
                          + '\n-- modification interdite\n',
                          encoding='utf-8')
        with pytest.raises(MigrationError, match='checksum'):
            run_migrations(dsn, directory=str(mod_dir), verbose=False)

    def test_contrat_additif_drop_refuse_base_intacte(self, server,
                                                      tmp_path):
        from statementflow.migrate import MigrationError, run_migrations
        dsn = _fresh_db(server, 'sfmigrate3')
        run_migrations(dsn, directory=MIGRATIONS, verbose=False)
        before = _objects(dsn)
        for payload in ('drop table users;', 'truncate organizations;',
                        'create role pirate;', 'alter system set x=1;'):
            bad_dir = tmp_path / f'mig-{abs(hash(payload)) % 99999}'
            bad_dir.mkdir()
            (bad_dir / '20261109999999_mechante.sql').write_text(
                payload, encoding='utf-8')
            with pytest.raises(MigrationError, match='interdit'):
                run_migrations(dsn, directory=str(bad_dir),
                               verbose=False)
        # la base est STRICTEMENT intacte après les tentatives
        assert _objects(dsn) == before

    def test_run_for_store_desactivable(self, server, monkeypatch):
        from statementflow.migrate import run_for_store
        dsn = _fresh_db(server, 'sfmigrate4')

        class FakeStore:
            _dsn = dsn

        monkeypatch.setenv('STATEMENTFLOW_AUTO_MIGRATE', '0')
        assert run_for_store(FakeStore(), verbose=False) == []
        # rien d'appliqué : pas même la table tracker
        import psycopg
        with psycopg.connect(dsn, autocommit=True) as c:
            exists = c.execute(
                "select exists(select 1 from pg_tables where"
                " tablename='schema_migrations')").fetchone()[0]
        assert exists is False
        monkeypatch.setenv('STATEMENTFLOW_AUTO_MIGRATE', '1')
        applied = run_for_store(FakeStore(), verbose=False)
        assert len(applied) == 5
        # échec non bloquant : DSN invalide → [] et pas d'exception
        class BadStore:
            _dsn = 'postgresql://nobody:nop@127.0.0.1:1/none'
        assert run_for_store(BadStore(), verbose=False) == []

    def test_resolution_repertoire(self, monkeypatch, tmp_path):
        from statementflow.migrate import migrations_dir
        monkeypatch.delenv('STATEMENTFLOW_MIGRATIONS_DIR', raising=False)
        # depuis la racine du dépôt
        monkeypatch.chdir(ROOT)
        assert migrations_dir() == os.path.join(
            ROOT, 'supabase', 'migrations')
        # variable d'environnement prioritaire (si le répertoire existe)
        custom = tmp_path / 'perso'
        custom.mkdir()
        monkeypatch.setenv('STATEMENTFLOW_MIGRATIONS_DIR', str(custom))
        assert migrations_dir() == str(custom)
        monkeypatch.delenv('STATEMENTFLOW_MIGRATIONS_DIR', raising=False)
