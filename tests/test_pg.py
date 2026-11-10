"""Tests de l'adaptateur PostgreSQL (v0.4.0) — exécutés contre un VRAI
serveur PostgreSQL embarqué (pgserver), avec les migrations RÉELLES du
dépôt appliquées dans l'ordre.

Couverture :
- application des 4 migrations SQL sur une base vierge + RLS deny-all ;
- émission de clé, réserve/settle/refund via les fonctions SQL (dont
  idempotence par job et solde jamais négatif) ;
- activation de rechargement transactionnelle et idempotente ;
- webhooks idempotents (rejeu → déjà vu) ;
- usage, jobs, purge, réconciliation, incohérences détectées ;
- isolation entre clés ;
- l'API complète branchée sur PostgreSQL : analyse facturée, achat,
  paiement simulé, webhook signé, double-credit impossible, isolation ;
- tâches de maintenance (cron) et verrou consultatif.

Ces tests sont IGNORÉS si pgserver/psycopg ne sont pas installés : le
dépôt reste installable sans dépendance PostgreSQL (mode JSON pur).
"""
import base64
import os
import shutil

import pytest

import os as _os
if _os.environ.get('SF_PG_TEST_URI'):
    # serveur PostgreSQL externe (CI : service conteneurisé) — psycopg
    # suffit, pgserver n'est pas utilisé
    pytest.importorskip('psycopg')
    pytest.importorskip('psycopg_pool')
    pgserver = None
else:
    # serveur embarqué local — pgserver requis
    pgserver = pytest.importorskip('pgserver')
    pytest.importorskip('psycopg')
    pytest.importorskip('psycopg_pool')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATIONS = os.path.join(ROOT, 'supabase', 'migrations')
PGDATA_PARENT = '/tmp/sf-pgserver'
SOCKET_DIR = '/tmp/statementflow'   # défaut pgserver — sous /tmp (sandbox)


class _ExternalServer:
    """Adaptateur pour un PostgreSQL externe déjà démarré (CI : service
    conteneurisé) — l'URI est fournie par SF_PG_TEST_URI."""

    def __init__(self, uri: str):
        self._uri = uri

    def get_uri(self) -> str:
        return self._uri

    def cleanup(self):
        pass


@pytest.fixture(scope='session')
def pg_server():
    """Serveur PostgreSQL pour la session pytest.

    Trois modes :
    - SF_PG_TEST_URI défini (CI) : PostgreSQL externe (service
      conteneurisé GitHub Actions / serveur déjà démarré), utilisé
      tel quel ;
    - exécution via tests/run_pg_tests.py : serveur pgserver vivant
      dans CE processus, réutilisé tel quel ;
    - exécution pytest directe : démarrage autonome pgserver (vit tant
      que la session pytest vit, arrêté via atexit).
    """
    external = os.environ.get('SF_PG_TEST_URI')
    if external:
        return _ExternalServer(external)
    if os.path.exists('/tmp/sf-pgserver/.s.PGSQL.5432'):
        import pgserver as _pgs
        return _pgs.get_server(PGDATA_PARENT)  # déjà démarré (même proc.)
    shutil.rmtree(PGDATA_PARENT, ignore_errors=True)
    shutil.rmtree(SOCKET_DIR, ignore_errors=True)
    server = pgserver.get_server(PGDATA_PARENT)
    import atexit
    atexit.register(server.cleanup)
    return server


def _db_uri(base_uri: str, dbname: str) -> str:
    """Remplace le nom de base dans une URI pgserver (le socket est passé
    en query param `host=`, un simple rsplit casserait l'URI)."""
    if '?' in base_uri:
        path, _, query = base_uri.partition('?')
        return path.rstrip('/').rsplit('/', 1)[0] + '/' + dbname + '?' + query
    return base_uri.rstrip('/').rsplit('/', 1)[0] + '/' + dbname


@pytest.fixture(scope='session')
def pg_db_uri(pg_server):
    """Base « statementflow » dédiée, migrations 001→004 appliquées."""
    import psycopg
    uri = pg_server.get_uri()
    with psycopg.connect(uri, autocommit=True) as c:
        exists = c.execute(
            "select 1 from pg_database where datname = 'statementflow'"
        ).fetchone()
        if not exists:
            c.execute('create database statementflow')
    db_uri = _db_uri(uri, 'statementflow')
    with psycopg.connect(db_uri, autocommit=True) as c:
        for name in sorted(os.listdir(MIGRATIONS)):
            if not name.endswith('.sql'):
                continue
            with open(os.path.join(MIGRATIONS, name),
                      encoding='utf-8') as f:
                c.execute(f.read())
    return db_uri


@pytest.fixture()
def pg_store(pg_db_uri):
    """PgStore neuf sur une base nettoyée (aucune fuite entre tests)."""
    import psycopg
    with psycopg.connect(pg_db_uri, autocommit=True) as c:
        c.execute('truncate organizations cascade')
    from statementflow.pg import PgStore
    store = PgStore(pg_db_uri, pool_min=0, pool_max=2)
    yield store
    store.close()


def _psycopg(pg_store):
    import psycopg
    return psycopg, pg_store.dsn


class TestMigrations:
    def test_all_tables_rls_active(self, pg_db_uri):
        """RLS actif sur les 8 tables (deny-all : aucune policy)."""
        import psycopg
        with psycopg.connect(pg_db_uri, autocommit=True) as c:
            tables = c.execute(
                "select tablename, rowsecurity from pg_tables"
                " where schemaname = 'public' order by tablename").fetchall()
            policies = c.execute(
                "select count(*) from pg_policies"
                " where schemaname = 'public'").fetchone()[0]
        by_name = dict(tables)
        for t in ('api_keys', 'audit_log', 'credit_ledger', 'jobs',
                  'organizations', 'topups', 'usage_counters',
                  'webhook_events'):
            assert by_name.get(t) is True, f'RLS inactif sur {t}'
        assert policies == 0

    def test_verification_queries_pass(self, pg_db_uri):
        """Le scénario de la migration 003 passe avec les correctifs 004 :
        réservation sans solde refusée, activation idempotente, solde
        exact, double settle sans erreur, webhook rejoué détecté."""
        import psycopg
        with psycopg.connect(pg_db_uri, autocommit=True) as c:
            c.execute('truncate organizations cascade')
            c.execute("begin")
            org = c.execute(
                "insert into organizations (name)"
                " values ('verif-003') returning id").fetchone()[0]
            c.execute(
                "insert into api_keys (org_id, key_hash, label)"
                " values (%s, repeat('b', 64), 'cle-verif')", (org,))
            assert c.execute(
                "select reserve_credits(repeat('b', 64), 1, 'job-v1')"
            ).fetchone()[0] is False          # sans solde → refus
            c.execute(
                "insert into topups (reference, key_id, pack, credits,"
                " amount_fcfa, session_id)"
                " select 'TOPUP-VERIF', id, 'starter', 500, 5000, 's-v'"
                " from api_keys where key_hash = repeat('b', 64)")
            assert c.execute(
                "select activate_topup('TOPUP-VERIF', 'tx-v1')"
            ).fetchone()[0] is True
            assert float(c.execute(
                "select credits_balance from api_key_balances"
                " where key_hash = repeat('b', 64)").fetchone()[0]) == 500
            # double activation → false, solde inchangé
            assert c.execute(
                "select activate_topup('TOPUP-VERIF', 'tx-v1')"
            ).fetchone()[0] is False
            assert float(c.execute(
                "select credits_balance from api_key_balances"
                " where key_hash = repeat('b', 64)").fetchone()[0]) == 500
            # réservation + échec → restitué ; rejeu idempotent
            assert c.execute(
                "select reserve_credits(repeat('b', 64), 1, 'job-v2')"
            ).fetchone()[0] is True
            assert float(c.execute(
                "select settle_credits(repeat('b', 64), 0, 'job-v2', false)"
            ).fetchone()[0]) == 500
            assert float(c.execute(
                "select settle_credits(repeat('b', 64), 0, 'job-v2', false)"
            ).fetchone()[0]) == 500
            # webhook rejoué
            assert c.execute(
                "select record_webhook_event('transaction.success:tx-v1')"
            ).fetchone()[0] is True
            assert c.execute(
                "select record_webhook_event('transaction.success:tx-v1')"
            ).fetchone()[0] is False
            c.execute('rollback')


class TestPgStoreKeys:
    def test_issue_lookup_balance_revoke(self, pg_store):
        key, rec = pg_store.issue('client-alpha', credits=10)
        assert key.startswith('sf_')
        kh = rec['key_hash']
        assert len(kh) == 64
        assert pg_store.balance(kh) == 10
        assert pg_store.lookup(kh)['credits'] == 10
        assert pg_store.is_revoked(kh) is False
        assert pg_store.revoke(kh) is True
        assert pg_store.lookup(kh) is None           # plus valide
        assert pg_store.is_revoked(kh) is True
        assert pg_store.balance(kh) == 10            # solde conservé
        assert pg_store.revoke(kh) is False

    def test_reserve_settle_idempotent(self, pg_store):
        _, rec = pg_store.issue('client-beta', credits=5)
        kh = rec['key_hash']
        assert pg_store.reserve(kh, 1.0, 'job-a') is True
        assert pg_store.balance(kh) == 4
        # re-réservation du même job : idempotente, rien débité
        assert pg_store.reserve(kh, 1.0, 'job-a') is True
        assert pg_store.balance(kh) == 4
        # coût réel 3 (réservé 1 + complément 2)
        assert pg_store.settle(kh, 2.0, 'job-a') == 2
        # settle rejoué : solde inchangé, pas d'erreur
        assert pg_store.settle(kh, 2.0, 'job-a') == 2
        assert pg_store.balance(kh) == 2

    def test_reserve_insufficient(self, pg_store):
        _, rec = pg_store.issue('client-delta', credits=0)
        assert pg_store.reserve(rec['key_hash'], 1.0, 'job-b') is False
        assert pg_store.balance(rec['key_hash']) == 0

    def test_refund_restores_reservation(self, pg_store):
        _, rec = pg_store.issue('client-gamma', credits=5)
        kh = rec['key_hash']
        assert pg_store.reserve(kh, 1.0, 'job-c') is True
        assert pg_store.balance(kh) == 4
        assert pg_store.refund(kh, 1.0, 'job-c') is True
        assert pg_store.balance(kh) == 5
        # refund rejoué : idempotent
        assert pg_store.refund(kh, 1.0, 'job-c') is True
        assert pg_store.balance(kh) == 5

    def test_settle_never_negative(self, pg_store):
        """Coût réel supérieur au solde restant : plafonné, jamais négatif."""
        _, rec = pg_store.issue('client-cap', credits=2)
        kh = rec['key_hash']
        assert pg_store.reserve(kh, 1.0, 'job-d') is True
        # coût réel 10 avec 2 crédits : débit plafonné à 1 restant
        pg_store.settle(kh, 9.0, 'job-d')
        assert pg_store.balance(kh) == 0

    def test_add_credits_never_negative(self, pg_store):
        _, rec = pg_store.issue('client-eps', credits=3)
        kh = rec['key_hash']
        pg_store.add_credits(kh, -9)                 # plafonné au disponible
        assert pg_store.balance(kh) == 0
        pg_store.add_credits(kh, 5)
        assert pg_store.balance(kh) == 5

    def test_keys_isolated(self, pg_store):
        _, r1 = pg_store.issue('iso-a', credits=10)
        _, r2 = pg_store.issue('iso-b', credits=10)
        assert pg_store.reserve(r1['key_hash'], 4, 'job-iso') is True
        assert pg_store.balance(r1['key_hash']) == 6
        assert pg_store.balance(r2['key_hash']) == 10

    def test_usage_counters(self, pg_store):
        _, rec = pg_store.issue('client-usage', credits=1)
        kh = rec['key_hash']
        assert pg_store.usage(kh) == {'documents': 0, 'pages': 0,
                                      'transactions': 0}
        pg_store.record_usage(kh, 3, 42)
        pg_store.record_usage(kh, 2, 10)
        assert pg_store.usage(kh) == {'documents': 2, 'pages': 5,
                                      'transactions': 52}
        snap = pg_store.usage_snapshot()
        assert snap[kh] == {'documents': 2, 'pages': 5, 'transactions': 52}
        pg_store.usage_reset(kh)
        assert pg_store.usage(kh)['documents'] == 0


class TestPgStoreTopups:
    def test_topup_lifecycle(self, pg_store):
        _, rec = pg_store.issue('client-pay', credits=0)
        kh = rec['key_hash']
        t = pg_store.create_topup(kh, 'starter',
                                  {'id': 'sess-1',
                                   'checkout_url': 'https://pay.test/s1'})
        assert t['status'] == 'pending'
        assert t['credits'] == 500 and t['amount_fcfa'] == 5000
        activated = pg_store.activate(t['reference'], 'tx-1')
        assert activated['status'] == 'activated'
        assert activated['transaction_id'] == 'tx-1'
        assert pg_store.balance(kh) == 500
        # ré-activation (même transaction OU autre) : jamais deux crédits
        assert pg_store.activate(t['reference'], 'tx-1') is None
        assert pg_store.activate(t['reference'], 'tx-2') is None
        assert pg_store.balance(kh) == 500
        # échec d'un autre rechargement : aucun crédit
        t2 = pg_store.create_topup(kh, 'pro', {'id': 'sess-2'})
        pg_store.mark_failed(t2['reference'], 'tx-f')
        assert pg_store.get(t2['reference'])['status'] == 'failed'
        assert pg_store.balance(kh) == 500

    def test_webhook_idempotence(self, pg_store):
        pg_store.issue('client-wh', credits=0)
        ev = 'transaction.success:tx-wh-1'
        assert pg_store.mark_webhook(ev, {'x': 1}) is True
        assert pg_store.mark_webhook(ev, {'x': 1}) is False
        assert pg_store.webhook_seen(ev) is True
        assert pg_store.webhook_seen('transaction.success:tx-wh-2') is False

    def test_expired_pending_topups(self, pg_store):
        psycopg, dsn = _psycopg(pg_store)
        _, rec = pg_store.issue('client-old', credits=0)
        t = pg_store.create_topup(rec['key_hash'], 'starter', {'id': 's-old'})
        assert pg_store.expired_pending_topups(older_than_hours=24) == []
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(
                "update topups set created_at = now() - interval '30 hours'"
                " where reference = %s", (t['reference'],))
        rows = pg_store.expired_pending_topups(older_than_hours=24)
        assert len(rows) == 1 and rows[0]['reference'] == t['reference']


class TestPgStoreJobsAndIntegrity:
    def test_record_job_and_purge(self, pg_store):
        psycopg, dsn = _psycopg(pg_store)
        _, rec = pg_store.issue('client-job', credits=5)
        kh = rec['key_hash']
        j1, j2 = '0123456789abcdef0123456789abcdef', \
                 'fedcba9876543210fedcba9876543210'
        pg_store.record_job(j1, kh, 'releve.pdf', 'pdf', 'done', pages=3,
                            transactions=40, credits_charged=3)
        pg_store.record_job(j2, kh, 'export.csv', 'csv', 'done', pages=1,
                            rows=100, transactions=90, credits_charged=2)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(
                "update jobs set expires_at = now() - interval '1 second'"
                " where id = %s::uuid", (j1,))
        assert pg_store.purge_expired_jobs() >= 1
        with psycopg.connect(dsn, autocommit=True) as c:
            n = c.execute('select count(*) from jobs').fetchone()[0]
        assert n == 1

    def test_purge_old_webhook_events(self, pg_store):
        psycopg, dsn = _psycopg(pg_store)
        pg_store.issue('client-pw', credits=0)
        pg_store.mark_webhook('transaction.success:old-1')
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(
                "update webhook_events"
                " set received_at = now() - interval '40 days'"
                " where event_key = 'transaction.success:old-1'")
        assert pg_store.purge_old_webhook_events(30) >= 1
        assert pg_store.webhook_seen('transaction.success:old-1') is False

    def test_integrity_detection(self, pg_store):
        psycopg, dsn = _psycopg(pg_store)
        _, rec = pg_store.issue('client-rec', credits=0)
        kh = rec['key_hash']
        t = pg_store.create_topup(kh, 'starter', {'id': 's'})
        pg_store.activate(t['reference'], 'tx-r1')
        assert pg_store.reconcile_topups() == []
        assert pg_store.orphan_credits() == []
        assert pg_store.negative_balances() == []
        # incohérence 1 : crédit sans paiement valide
        with psycopg.connect(dsn, autocommit=True) as c:
            kid = c.execute(
                'select id from api_keys where key_hash = %s',
                (kh,)).fetchone()[0]
            c.execute(
                "insert into credit_ledger (key_id, amount, kind,"
                " idempotency_key, reference_id)"
                " values (%s, 250, 'topup', 'topup:TOPUP-FAKE', 'TOPUP-FAKE')",
                (kid,))
        assert len(pg_store.orphan_credits()) == 1
        # incohérence 2 : topup activé sans mouvement de crédit
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(
                "delete from credit_ledger where idempotency_key = %s",
                ('topup:' + t['reference'],))
        assert len(pg_store.reconcile_topups()) == 1

    def test_ping(self, pg_store):
        assert pg_store.ping() is True

    def test_audit_log(self, pg_store):
        _, rec = pg_store.issue('client-audit', credits=0)
        pg_store.audit('maintenance.run', {'ok': True},
                       key_hash=rec['key_hash'])
        recent = pg_store.audit_recent(limit=5)
        assert any(a['event'] == 'maintenance.run' for a in recent)


class TestEnvSelection:
    def test_pgstore_from_env(self, monkeypatch):
        from statementflow.pg import pgstore_from_env
        monkeypatch.delenv('STATEMENTFLOW_DATABASE_URL', raising=False)
        store, mode = pgstore_from_env()
        assert store is None and mode == 'json'
        monkeypatch.setenv('STATEMENTFLOW_DATABASE_URL',
                           'postgresql://u:p@h/db')
        store, mode = pgstore_from_env()
        assert mode == 'postgres' and store is not None

    def test_mask_dsn_hides_credentials(self):
        from statementflow.pg import _mask_dsn
        masked = _mask_dsn(
            'postgresql://user:secret@db.example.com:5432/postgres')
        assert 'secret' not in masked
        assert 'db.example.com' in masked


class TestPgApiIntegration:
    """L'API complète branchée sur PostgreSQL (mêmes routes, autre
    persistance) : analyse facturée → achat → paiement simulé → webhook
    signé → activation transactionnelle → rejeu sans double crédit."""

    @pytest.fixture()
    def pg_api(self, monkeypatch, pg_db_uri):
        import psycopg
        with psycopg.connect(pg_db_uri, autocommit=True) as c:
            c.execute('truncate organizations cascade')
        from statementflow import api as api_mod
        from statementflow.billing import FakeSasPay
        from statementflow.pg import PgStore, PgUsageStore
        store = PgStore(pg_db_uri, pool_min=0, pool_max=2)
        monkeypatch.setattr(api_mod, 'managed_keys', store)
        monkeypatch.setattr(api_mod, 'billing_store', store)
        monkeypatch.setattr(api_mod, 'usage_store', PgUsageStore(store))
        monkeypatch.setattr(api_mod, 'saspay',
                            FakeSasPay(webhook_secret='whsec-pgtest'))
        monkeypatch.setenv('STATEMENTFLOW_SASPAY_WEBHOOK_SECRET',
                           'whsec-pgtest')
        monkeypatch.setenv('STATEMENTFLOW_TEST_PAYMENT', '1')
        api_mod._jobs.clear()
        from fastapi.testclient import TestClient
        client = TestClient(api_mod.app)
        yield client, store
        store.close()

    def test_analyze_billed_on_postgres(self, pg_api):
        client, store = pg_api
        key, rec = store.issue('pilote-pg', credits=2)
        csv = (b'Date;Libelle;Debit;Credit;Solde\n'
               b'01/02/2026;A;100;;900\n'
               b'02/02/2026;B;;50;950\n'
               b'03/02/2026;C;10;;940\n')
        r = client.post('/v1/analyze', files={'file': ('t.csv', csv)},
                        headers={'X-API-Key': key})
        assert r.status_code == 202
        job_id = r.json()['job_id']
        r2 = client.get(f'/v1/results/{job_id}', headers={'X-API-Key': key})
        assert r2.status_code == 200
        billing = r2.json()['billing']
        assert billing['billing_mode'] == 'managed'
        assert billing['credits_charged'] == 1
        assert billing['credits_remaining'] == 1
        kh = rec['key_hash']
        assert store.usage(kh)['documents'] == 1
        assert store.usage(kh)['transactions'] == 3
        # métadonnées du job persistées (traçabilité) — vérifiées dans
        # test_record_job_and_purge (le branchement API les écrit)
        # échec : réservation restituée
        r3 = client.post('/v1/analyze',
                         files={'file': ('x.pdf', b'%PDF-fake')},
                         headers={'X-API-Key': key})
        assert r3.status_code == 202
        client.get(f"/v1/results/{r3.json()['job_id']}",
                   headers={'X-API-Key': key})
        assert store.balance(kh) == 1

    def test_full_topup_flow_on_postgres(self, pg_api):
        client, store = pg_api
        key, rec = store.issue('acheteur-pg', credits=1)
        kh = rec['key_hash']
        r = client.post('/v1/billing/topup',
                        json={'pack': 'starter',
                              'customer_email': 'a@b.co',
                              'customer_name': 'A B'},
                        headers={'X-API-Key': key})
        assert r.status_code == 200
        ref = r.json()['reference']
        # pending tant que non payé
        r4 = client.get(f'/v1/billing/status/{ref}',
                        headers={'X-API-Key': key})
        assert r4.status_code == 200
        assert r4.json()['status'] == 'pending'
        # paiement simulé → webhook signé → activation
        sid = store.get(ref)['session_id']
        r5 = client.post('/v1/billing/_test/pay',
                         json={'session_id': sid, 'success': True})
        assert r5.status_code == 200
        wb = r5.json()
        body = base64.b64decode(wb['webhook_body_b64'])
        wh_headers = {'X-Webhook-Signature': wb['webhook_signature'],
                      'X-Webhook-Timestamp': wb['webhook_timestamp'],
                      'X-Webhook-Event': wb['event'],
                      'Content-Type': 'application/json'}
        r6 = client.post('/v1/billing/webhook', content=body,
                         headers=wh_headers)
        assert r6.status_code == 200
        assert r6.json()['activated'] is True
        assert store.balance(kh) == 501     # 1 restant + 500 du pack
        # rejeu du webhook : duplicate, AUCUN double crédit
        r7 = client.post('/v1/billing/webhook', content=body,
                         headers=wh_headers)
        assert r7.status_code == 200
        assert r7.json().get('duplicate') is True
        assert store.balance(kh) == 501
        # statut final
        r8 = client.get(f'/v1/billing/status/{ref}',
                        headers={'X-API-Key': key})
        assert r8.json()['status'] == 'activated'
        # isolation : une autre clé ne voit PAS ce rechargement
        other_key, _ = store.issue('intrus', credits=0)
        r9 = client.get(f'/v1/billing/status/{ref}',
                        headers={'X-API-Key': other_key})
        assert r9.status_code == 404

    def test_webhook_invalid_signature_rejected(self, pg_api):
        client, store = pg_api
        key, _ = store.issue('cible-sig', credits=0)
        r = client.post('/v1/billing/webhook',
                        content=b'{"event":"transaction.success","data":{}}',
                        headers={'X-Webhook-Signature': 'deadbeef',
                                 'X-Webhook-Timestamp': '1',
                                 'X-Webhook-Event': 'transaction.success',
                                 'Content-Type': 'application/json'})
        assert r.status_code == 401


class TestMaintenance:
    def test_run_all_tasks_nominal(self, pg_store, capsys):
        from statementflow.billing import FakeSasPay
        from statementflow.maintenance import runMaintenance
        _, rec = pg_store.issue('client-maint', credits=5)
        pg_store.reserve(rec['key_hash'], 1.0, 'job-m1')
        rc = runMaintenance(pg_store, FakeSasPay(), 'test', report=None)
        out = capsys.readouterr().out
        assert rc == 0
        assert 'cleanup' in out and 'health' in out
        assert 'postgres_ok' in out
        # audit journalisé
        assert any(a['event'] == 'maintenance.run'
                   for a in pg_store.audit_recent(limit=3))

    def test_reconciliation_activates_paid_topup(self, pg_store):
        """Un paiement confirmé côté passerelle mais manqué par le webhook
        est rattrapé par la réconciliation (activation transactionnelle)."""
        from statementflow.billing import FakeSasPay
        from statementflow.maintenance import runMaintenance
        gw = FakeSasPay()
        _, rec = pg_store.issue('client-reconcile', credits=0)
        kh = rec['key_hash']
        session = gw.create_checkout(5000, 'test', 'a@b.co', 'A B')
        t = pg_store.create_topup(kh, 'starter', session)
        gw.pay_session(session['id'], success=True)
        psycopg_mod = __import__('psycopg')
        with psycopg_mod.connect(pg_store.dsn, autocommit=True) as c:
            c.execute("update topups set created_at ="
                      " now() - interval '2 hours'"
                      " where reference = %s", (t['reference'],))
        rc = runMaintenance(pg_store, gw, 'saspay')
        assert rc == 0
        assert pg_store.get(t['reference'])['status'] == 'activated'
        assert pg_store.balance(kh) == 500
        # rejouer : idempotent, pas de double crédit
        rc = runMaintenance(pg_store, gw, 'saspay')
        assert rc == 0
        assert pg_store.balance(kh) == 500

    def test_reconciliation_marks_failed(self, pg_store):
        from statementflow.billing import FakeSasPay
        from statementflow.maintenance import runMaintenance
        gw = FakeSasPay()
        _, rec = pg_store.issue('client-failrec', credits=0)
        session = gw.create_checkout(5000, 'test', 'a@b.co', 'A B')
        t = pg_store.create_topup(rec['key_hash'], 'starter', session)
        gw.pay_session(session['id'], success=False)
        psycopg_mod = __import__('psycopg')
        with psycopg_mod.connect(pg_store.dsn, autocommit=True) as c:
            c.execute("update topups set created_at ="
                      " now() - interval '2 hours'"
                      " where reference = %s", (t['reference'],))
        rc = runMaintenance(pg_store, gw, 'saspay')
        assert rc == 0
        assert pg_store.get(t['reference'])['status'] == 'failed'
        assert pg_store.balance(rec['key_hash']) == 0

    def test_integrity_critical_exit_code(self, pg_store, capsys):
        from statementflow.billing import FakeSasPay
        from statementflow.maintenance import runMaintenance
        psycopg_mod = __import__('psycopg')
        _, rec = pg_store.issue('client-crit', credits=0)
        kh = rec['key_hash']
        with psycopg_mod.connect(pg_store.dsn, autocommit=True) as c:
            kid = c.execute('select id from api_keys where key_hash = %s',
                            (kh,)).fetchone()[0]
            c.execute(
                "insert into credit_ledger (key_id, amount, kind,"
                " idempotency_key, reference_id)"
                " values (%s, 100, 'topup', 'topup:TOPUP-GHOST',"
                " 'TOPUP-GHOST')", (kid,))
        rc = runMaintenance(pg_store, FakeSasPay(), 'test')
        assert rc == 2                      # anomalie critique signalée
        err = capsys.readouterr().err
        assert 'CRITIQUE' in err

    def test_main_without_database(self, monkeypatch):
        from statementflow.maintenance import main
        monkeypatch.delenv('STATEMENTFLOW_DATABASE_URL', raising=False)
        assert main([]) == 0                # mode JSON : rien à faire
