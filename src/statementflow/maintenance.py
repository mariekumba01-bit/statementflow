"""Tâches périodiques StatementFlow (Cron Job Render, v0.4.0).

Exécution : python -m statementflow.maintenance
(dockerCommand du service cron render.yaml — image identique à l'API).

Quatre tâches, toutes idempotentes et sûres à rejouer :

A. Nettoyage — purge des métadonnées de jobs expirées (TTL), des
   événements webhook de plus de 30 jours. Les résultats bancaires ne
   sont jamais stockés (traitement éphémère) : rien d'autre à purger.

B. Réconciliation SasPay — pour chaque rechargement pending de plus de
   N heures : interrogation du statut RÉEL chez la passerelle (jamais un
   statut mémorisé) ; activation des crédits si PAID, échec si FAILED,
   laissé en attente sinon. Un paiement manqué par le webhook est ainsi
   rattrapé (filet de sécurité documenté SasPay : le polling).

C. Intégrité financière — détection (SANS correction automatique) :
   soldes négatifs, rechargements activés sans crédit au grand livre,
   crédits sans paiement valide. Toute incohérence est signalée dans le
   rapport et le journal d'audit — la correction reste humaine.

D. Surveillance — santé PostgreSQL, mode passerelle, compteurs ;
   journalisation de l'exécution dans audit_log ; code de sortie non
   nul si une anomalie critique est détectée (alerte Render).

Fréquence recommandée : horaire ('15 * * * *'). Justification : les
renvois de webhook SasPay s'étalent sur ~2 h 30 (immédiat, +30 s, +5 min,
+30 min, +2 h) ; une réconciliation horaire rattrape tout événement
manqué en moins d'une heure, et le polling client (/v1/billing/status)
reste le mécanisme temps réel. Le nettoyage serait suffisant au
quotidien, mais il est gratuit (requêtes indexées) et idempotent.

Verrou d'exécution : pg_try_advisory_lock sur une connexion DÉDIÉE
(hors pool) — deux exécutions simultanées sont impossible ; une
exécution interrompue libère le verrou avec sa connexion.

Variables d'environnement : STATEMENTFLOW_DATABASE_URL (requis — sans
PostgreSQL, il n'y a rien à réconcilier : sortie 0 avec avertissement),
STATEMENTFLOW_SASPAY_SECRET_KEY (réconciliation réelle ; sans elle, la
tâche B est sautée en mode test), STATEMENTFLOW_RECONCILE_AFTER_HOURS
(défaut 1 : réconcilier les pending de plus d'1 heure), exit code 2 si
anomalie critique. Aucun secret n'est journalisé.
"""
from __future__ import annotations

import json
import sys
import time
from typing import Optional

from .billing import SasPayError, is_failed, is_paid, provider_from_env
from .pg import PgStore, pgstore_from_env

ADVISORY_LOCK_KEY = 'statementflow-maintenance'
DEFAULT_RECONCILE_AFTER_HOURS = 1
WEBHOOK_EVENT_RETENTION_DAYS = 30


class MaintenanceReport:
    """Rapport d'exécution structuré (logs exploitables + machine)."""

    def __init__(self):
        self.started_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        self.tasks: list[dict] = []
        self.critical: list[str] = []

    def task(self, name: str, detail: dict, status: str = 'ok'):
        self.tasks.append({'task': name, 'status': status,
                           'detail': detail})
        flag = '!!' if status != 'ok' else ' ·'
        print(f'{flag} {name} : {json.dumps(detail, ensure_ascii=False)}',
              flush=True)

    def critical_issue(self, message: str):
        self.critical.append(message)
        print(f'!! CRITIQUE : {message}', file=sys.stderr, flush=True)

    def summary(self) -> dict:
        return {'started_at': self.started_at,
                'finished_at': time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                             time.gmtime()),
                'tasks': self.tasks,
                'critical_issues': self.critical}

    @property
    def exit_code(self) -> int:
        return 2 if self.critical else 0


# ── tâche A : nettoyage ────────────────────────────────────────────────
def task_cleanup(store: PgStore, report: MaintenanceReport) -> None:
    jobs_purged = store.purge_expired_jobs()
    events_purged = store.purge_old_webhook_events(
        WEBHOOK_EVENT_RETENTION_DAYS)
    report.task('cleanup', {
        'jobs_expired_purged': jobs_purged,
        'webhook_events_purged': events_purged,
        'webhook_retention_days': WEBHOOK_EVENT_RETENTION_DAYS,
    })


# ── tâche B : réconciliation SasPay ────────────────────────────────────
def task_reconcile_saspay(store: PgStore, saspay, mode: str,
                          after_hours: int,
                          report: MaintenanceReport) -> None:
    if mode != 'saspay':
        report.task('reconcile_saspay', {
            'skipped': True,
            'reason': 'mode test (aucune STATEMENTFLOW_SASPAY_SECRET_KEY) :'
                      ' rien à réconcilier chez une passerelle simulée',
        })
        return
    pending = store.expired_pending_topups(older_than_hours=after_hours)
    activated, failed, still_pending, unreachable = [], [], [], []
    for t in pending:
        try:
            status = saspay.checkout_status(t['session_id'])
        except SasPayError as e:
            unreachable.append({'reference': t['reference'],
                                'error': str(e)[:120]})
            continue
        if is_paid(status):
            # activation via la MÊME fonction transactionnelle que le
            # webhook : idempotente par construction (statut + transaction)
            record = store.activate(t['reference'],
                                    status.get('transaction_id') or '')
            if record is not None:
                activated.append(t['reference'])
        elif is_failed(status):
            store.mark_failed(t['reference'],
                              status.get('transaction_id') or '')
            failed.append(t['reference'])
        else:
            still_pending.append(t['reference'])
    detail = {'checked': len(pending), 'activated': activated,
              'marked_failed': failed, 'still_pending': still_pending,
              'gateway_unreachable': unreachable}
    # des sessions injoignables répétées signalent un problème passerelle
    status = 'ok' if not unreachable else 'warn'
    report.task('reconcile_saspay', detail, status)


# ── tâche C : intégrité financière ─────────────────────────────────────
def task_integrity(store: PgStore, report: MaintenanceReport) -> None:
    negatives = store.negative_balances()
    missing_credit = store.reconcile_topups()
    orphan_credit = store.orphan_credits()
    report.task('integrity', {
        'negative_balances': negatives,
        'activated_topups_without_credit': missing_credit,
        'credits_without_valid_payment': orphan_credit,
    })
    # ces incohérences sont CRITIQUES : elles touchent l'argent réel et ne
    # sont jamais corrigées automatiquement (intervention humaine requise)
    if negatives:
        report.critical_issue(
            f'{len(negatives)} solde(s) négatif(s) — théoriquement '
            f'impossible, inspecter le grand livre')
    if missing_credit:
        report.critical_issue(
            f'{len(missing_credit)} rechargement(s) activé(s) sans crédit '
            f'au grand livre : {", ".join(t["reference"] for t in missing_credit)}')
    if orphan_credit:
        report.critical_issue(
            f'{len(orphan_credit)} crédit(s) attribué(s) sans paiement '
            f'valide correspondant')


# ── tâche D : surveillance ─────────────────────────────────────────────
def task_health(store: PgStore, mode: str, report: MaintenanceReport) -> None:
    db_ok = store.ping()
    report.task('health', {'postgres_ok': db_ok, 'gateway_mode': mode})
    if not db_ok:
        report.critical_issue('PostgreSQL injoignable')


def runMaintenance(store: PgStore, saspay, mode: str,
                   after_hours: int = DEFAULT_RECONCILE_AFTER_HOURS,
                   report: Optional[MaintenanceReport] = None) -> int:
    """Exécute les quatre tâches et journalise le rapport. Retourne le
    code de sortie (0 nominal, 2 anomalie critique)."""
    report = report or MaintenanceReport()
    task_health(store, mode, report)
    task_cleanup(store, report)
    task_reconcile_saspay(store, saspay, mode, after_hours, report)
    task_integrity(store, report)
    # journal d'audit (traçabilité des exécutions) — meilleur effort :
    # une base injoignable ne doit pas masquer le rapport déjà imprimé
    try:
        store.audit('maintenance.run', report.summary())
    except Exception as e:
        print(f'· audit_log indisponible : {type(e).__name__}',
              file=sys.stderr, flush=True)
    return report.exit_code


def main(argv: Optional[list[str]] = None) -> int:
    import os
    print(f'StatementFlow maintenance — {time.strftime("%Y-%m-%dT%H:%M:%SZ")}'
          ' (UTC)', flush=True)
    store, mode = pgstore_from_env()
    if store is None:
        # sans PostgreSQL, aucune donnée persistante : rien à faire
        print('· STATEMENTFLOW_DATABASE_URL non défini : aucune donnée'
              ' persistante à maintenir (mode JSON). Sortie nominale.',
              flush=True)
        return 0
    try:
        try:
            after_hours = int(os.environ.get(
                'STATEMENTFLOW_RECONCILE_AFTER_HOURS',
                DEFAULT_RECONCILE_AFTER_HOURS))
        except ValueError:
            after_hours = DEFAULT_RECONCILE_AFTER_HOURS
        saspay, gw_mode = provider_from_env()
        # verrou anti-exécutions simultanées sur une connexion DÉDIÉE :
        # le verrou de session survit tant que la connexion est ouverte.
        # connect_direct() pré-valide le DSN (sanitisation + parsing) :
        # une erreur de format ne JAMAIS exposer la chaîne (mot de passe)
        # — le message libpq brut ne sort pas du store.
        from .pg import PgStoreError
        with store.connect_direct() as lock_conn:
            with lock_conn.cursor() as cur:
                locked = cur.execute(
                    'select pg_try_advisory_lock(hashtext(%s))',
                    (ADVISORY_LOCK_KEY,)).fetchone()[0]
            if not locked:
                print('· une exécution est déjà en cours (verrou consultatif)'
                      ' : sortie sans rien faire.', flush=True)
                return 0
            try:
                rc = runMaintenance(store, saspay, gw_mode, after_hours)
            finally:
                with lock_conn.cursor() as cur:
                    cur.execute('select pg_advisory_unlock(hashtext(%s))',
                                (ADVISORY_LOCK_KEY,))
                lock_conn.commit()
        return rc
    except PgStoreError as e:
        # erreur déjà sans secret (DSN masqué, type classifié)
        print(f'!! PostgreSQL indisponible : {e}', file=sys.stderr, flush=True)
        return 3
    except Exception as e:
        print(f'!! échec de la maintenance : {type(e).__name__}', 
              file=sys.stderr, flush=True)
        return 3
    finally:
        store.close()


if __name__ == '__main__':
    sys.exit(main())
