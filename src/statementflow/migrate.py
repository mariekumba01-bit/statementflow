"""Auto-migration contrôlée du schéma PostgreSQL au démarrage de l'API.

Principe (v0.5.1) : l'API possède le DSN (STATEMENTFLOW_DATABASE_URL)
sur Render ; les migrations vivent dans supabase/migrations/*.sql.
Au démarrage, quand PostgreSQL est actif, le schéma est mis à jour
SANS intervention humaine :

- chaque fichier est appliqué UNE SEULE FOIS, tracé dans la table
  `schema_migrations` (nom + somme de contrôle sha256) ;
- re-jeu sûr : tous les fichiers sont additifs et idempotents
  (create … if not exists / create or replace) — preuve : double
  application intégrale testée (tests/test_migrate.py) ;
- CONTRAT MÉCANIQUE d'additivité : tout énoncé destructeur (drop …,
  truncate, create/alter role, alter system) est REFUSÉ avant
  exécution — un tel fichier ne peut pas être appliqué, même par
  erreur ;
- un fichier déjà tracé dont le contenu aurait changé (somme de
  contrôle différente) est REFUSÉ : on ne modifie jamais une
  migration déjà appliquée, on en écrit une nouvelle ;
- la 003 est un jeu de vérification entièrement enveloppé dans
  begin; … rollback; — elle n'écrit RIEN (nette) ;
- échec : l'API NE s'arrête PAS (les conversions des clés existantes
  restent servies) ; l'erreur est journalisée bruyamment et les
  fonctionnalités dépendantes répondent 503 explicites.

Désactivation : STATEMENTFLOW_AUTO_MIGRATE=0.

Répertoire des migrations (ordre de résolution) :
1. STATEMENTFLOW_MIGRATIONS_DIR ;
2. ./supabase/migrations (dépôt, image Docker — WORKDIR /app) ;
3. ../supabase/migrations relatif au paquet (exécution depuis src/).

Aucune donnée existante n'est lue, modifiée ou supprimée par le
moteur lui-même ; il n'applique que des fichiers validés.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from typing import Optional

ENV_PREFIX = 'STATEMENTFLOW_'
_NAME_RE = re.compile(r'^\d{14}_[a-z0-9_]+\.sql$')

# Motifs REFUSÉS partout (jamais présents dans nos migrations —
# vérifié par test) : le moteur ne peut appliquer QUE de l'additif.
_FORBIDDEN = (
    'drop table', 'drop view', 'drop index', 'drop function',
    'drop column', 'drop database', 'drop schema', 'drop role',
    'create role', 'alter role', 'alter system', 'truncate ',
)

_TRACKER_DDL = """
create table if not exists schema_migrations (
    name        text primary key,
    checksum    text not null,
    applied_at  timestamptz not null default now()
)
"""


class MigrationError(RuntimeError):
    """Erreur de migration (message affichable, sans secret)."""


def migrations_dir(explicit: Optional[str] = None) -> Optional[str]:
    """Répertoire des migrations, ou None si introuvable."""
    candidates = []
    if explicit:
        candidates.append(explicit)
    env_dir = os.environ.get(ENV_PREFIX + 'MIGRATIONS_DIR', '').strip()
    if env_dir:
        candidates.append(env_dir)
    candidates.append(os.path.join(os.getcwd(), 'supabase', 'migrations'))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.normpath(os.path.join(
        here, '..', '..', 'supabase', 'migrations')))
    for d in candidates:
        if d and os.path.isdir(d):
            return d
    return None


def _check_safe(name: str, sql: str) -> None:
    """Refuse tout énoncé destructeur (contrat additif)."""
    lowered = sql.lower()
    for pattern in _FORBIDDEN:
        if pattern in lowered:
            raise MigrationError(
                f'{name} : énoncé interdit « {pattern.strip()} » — le '
                'moteur n\'applique que des migrations additives.')


def run_migrations(dsn: str, directory: Optional[str] = None,
                   verbose: bool = True) -> list[str]:
    """Applique les migrations non encore tracées. Retourne la liste
    des fichiers appliqués (vide si le schéma est déjà à jour).

    Lève MigrationError sur fichier destructeur, checksum modifié ou
    échec SQL. Ne journalise JAMAIS le DSN (le message d'erreur
    psycopg est tronqué : il ne contient pas le secret, mais par
    prudence seul son type remonte)."""
    import psycopg

    directory = migrations_dir(directory)
    if directory is None:
        if verbose:
            print('[migrate] aucun répertoire supabase/migrations trouvé '
                  '— schéma non vérifié', file=sys.stderr)
        return []

    files = sorted(f for f in os.listdir(directory)
                   if _NAME_RE.match(f))
    if not files:
        return []

    applied: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(_TRACKER_DDL)
        # un seul process applique à la fois (verrou consultatif) :
        # deux workers Render ne peuvent pas se marcher dessus
        locked = conn.execute(
            'select pg_try_advisory_lock(hashtext('
            "'statementflow-migrations'))").fetchone()[0]
        if not locked:
            if verbose:
                print('[migrate] un autre processus applique les '
                      'migrations — ignoré', file=sys.stderr)
            return []
        try:
            known = {row[0]: row[1] for row in conn.execute(
                'select name, checksum from schema_migrations').fetchall()}
            for name in files:
                sql = open(os.path.join(directory, name),
                           encoding='utf-8').read()
                _check_safe(name, sql)
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                if name in known:
                    if known[name] != checksum:
                        raise MigrationError(
                            f'{name} : contenu modifié après application '
                            '(checksum ≠) — écrire une NOUVELLE migration, '
                            'ne jamais éditer une migration appliquée.')
                    continue  # déjà appliquée, inchangée : rien à faire
                conn.execute(sql)
                conn.execute(
                    'insert into schema_migrations (name, checksum)'
                    ' values (%s, %s) on conflict (name) do nothing',
                    (name, checksum))
                applied.append(name)
                if verbose:
                    print(f'[migrate] appliquée : {name}', file=sys.stderr)
        finally:
            conn.execute(
                "select pg_advisory_unlock(hashtext("
                "'statementflow-migrations'))")
    if verbose and not applied:
        print(f'[migrate] schéma déjà à jour ({len(files)} migrations '
              'tracées)', file=sys.stderr)
    return applied


def run_for_store(store, verbose: bool = True) -> list[str]:
    """Point d'entrée utilisé par api.py : applique les migrations
    pour le PgStore actif. Les erreurs ne cassent PAS le démarrage
    (journalisées ; les routes dépendantes répondent 503), SAUF si
    STATEMENTFLOW_AUTO_MIGRATE=strict (échec bloquant, utile en CI)."""
    mode = os.environ.get(ENV_PREFIX + 'AUTO_MIGRATE', '1').strip().lower()
    if mode in ('0', 'false', 'off', 'no'):
        return []
    dsn = getattr(store, '_dsn', '')
    if not dsn:
        return []
    try:
        return run_migrations(dsn, verbose=verbose)
    except MigrationError as e:
        if mode == 'strict':
            raise
        print(f'[migrate] ÉCHEC (service continué sans ces migrations) : '
              f'{e}', file=sys.stderr)
        return []
    except Exception as e:  # psycopg, OSError…
        if mode == 'strict':
            raise
        print(f'[migrate] ÉCHEC technique ({type(e).__name__}) — service '
              'continué sans ces migrations', file=sys.stderr)
        return []
