"""Exécuteur des tests PostgreSQL contre un serveur embarqué (pgserver).

Lance un vrai PostgreSQL 16 dans /tmp, applique les migrations réelles
du dépôt (supabase/migrations/*.sql) sur une base « statementflow »,
puis exécute pytest DANS LE MÊME PROCESSUS (le serveur pgserver est
géré par un gestionnaire atexit : il vit tant que ce script vit).

Ce pont est nécessaire parce que le serveur pgserver s'arrête avec le
processus qui l'a démarré : un appel `pytest` séparé ne pourrait pas
s'y connecter. En CI (GitHub Actions), un service PostgreSQL natif est
utilisé à la place (voir .github/workflows/tests.yml) — ce fichier ne
sert qu'aux environnements de développement sans PostgreSQL installé.

Usage : python tests/run_pg_tests.py [-k selection]
"""
import atexit
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src'))
sys.path.insert(0, HERE)

PGDATA = '/tmp/sf-pgserver'
SOCKET_DIR = '/tmp/statementflow'


def main() -> int:
    import pgserver
    import psycopg

    # état propre (un reste d'une exécution tuée peut traîner en /tmp)
    shutil.rmtree(PGDATA, ignore_errors=True)
    shutil.rmtree(SOCKET_DIR, ignore_errors=True)

    server = pgserver.get_server(PGDATA)
    atexit.register(server.cleanup)

    uri = server.get_uri()
    with psycopg.connect(uri, autocommit=True) as c:
        if not c.execute(
                "select 1 from pg_database where datname = 'statementflow'"
        ).fetchone():
            c.execute('create database statementflow')
    # le socket est en query param (host=…) : remplacer proprement le
    # nom de base, pas un rsplit naïf qui casserait l'URI
    if '?' in uri:
        path, _, query = uri.partition('?')
        db_uri = (path.rstrip('/').rsplit('/', 1)[0]
                  + '/statementflow?' + query)
    else:
        db_uri = uri.rstrip('/').rsplit('/', 1)[0] + '/statementflow'
    with psycopg.connect(db_uri, autocommit=True) as c:
        migrations = os.path.join(ROOT, 'supabase', 'migrations')
        for name in sorted(os.listdir(migrations)):
            if name.endswith('.sql'):
                with open(os.path.join(migrations, name),
                          encoding='utf-8') as f:
                    c.execute(f.read())
    print(f'[run_pg_tests] PostgreSQL prêt : {db_uri}')

    # marquer la session pytest : les fixtures détectent le serveur déjà
    # démarré dans CE processus et ne le recréent pas
    os.environ['SF_PG_TEST_URI'] = db_uri

    import pytest
    args = [os.path.join(HERE, 'test_pg.py')] + sys.argv[1:]
    return pytest.main(args)


if __name__ == '__main__':
    sys.exit(main())
