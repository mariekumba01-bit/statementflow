"""Contrôles de cohérence comptable et calcul de confiance.

Règles vérifiées :
- chaîne des soldes : solde_précédent + crédit − débit = solde (tolérance) ;
- règle d'ensemble : ouverture + Σcrédits − Σdébits = clôture ;
- doublons : répétition de page (identique, page différente) supprimée,
  lignes strictement identiques adjacentes signalées sans suppression.
"""
from __future__ import annotations

from typing import Optional

from .core import Alert, Transaction


def amounts_equal(a: Optional[float], b: Optional[float], tol: float = 0.01) -> bool:
    """Égalité de montants avec tolérance.

    None ≡ 0 uniquement quand l'AUTRE valeur est aussi nulle (à ± tol) :
    « None » signifie « colonne vide », écrite « 0 » dans de nombreux
    exports — mais None ne doit jamais masquer un montant non nul.
    """
    av, bv = (0.0 if a is None else float(a)), (0.0 if b is None else float(b))
    if a is None or b is None:
        return abs(av - bv) <= tol and abs(av) <= tol and abs(bv) <= tol
    return abs(av - bv) <= tol


def deduplicate(transactions: list[Transaction]) -> tuple[list[Transaction], list[Alert]]:
    """Supprime les répétitions de page, signale les doublons adjacents.

    Une « répétition de page » est une transaction strictement identique
    (date, libellé, montants, solde) à la précédente mais extraite d'une
    autre page : certains relevés réimpriment la dernière ligne en haut de
    la page suivante. Les doublons au sein d'une même page sont conservés
    (cas légitime en comptabilité) mais signalés.
    """
    kept: list[Transaction] = []
    alerts: list[Alert] = []
    removed = 0
    for tx in transactions:
        if kept:
            prev = kept[-1]
            same = all([
                tx.date == prev.date,
                tx.label == prev.label,
                amounts_equal(tx.debit, prev.debit),
                amounts_equal(tx.credit, prev.credit),
                amounts_equal(tx.balance, prev.balance),
            ])
            if same and tx.page != prev.page:
                removed += 1
                alerts.append(Alert(
                    'duplicate_page_repeat', 'info',
                    f'Transaction identique à la précédente répétée en haut de la '
                    f'page {tx.page} (originale page {prev.page}) conservée une seule fois.',
                    transaction_index=prev.index))
                continue
            if same:
                alerts.append(Alert(
                    'possible_duplicate', 'warning',
                    'Deux transactions strictement identiques et adjacentes '
                    '(conservées toutes les deux : cas légitime possible).',
                    transaction_index=tx.index))
        kept.append(tx)
    # renumérotation
    for i, tx in enumerate(kept, 1):
        tx.index = i
    return kept, alerts


def compute_checks(transactions: list[Transaction],
                   opening: Optional[float],
                   closing: Optional[float],
                   tolerance: float = 0.01):
    """Contrôles de cohérence → (checks, alerts)."""
    alerts: list[Alert] = []
    total_debit = sum(t.debit or 0.0 for t in transactions)
    total_credit = sum(t.credit or 0.0 for t in transactions)

    # chaîne des soldes
    chain_matches = 0
    chain_breaks = []
    prev_balance = opening
    for tx in transactions:
        if tx.balance is None or prev_balance is None:
            prev_balance = tx.balance if tx.balance is not None else prev_balance
            continue
        expected = prev_balance + (tx.credit or 0.0) - (tx.debit or 0.0)
        if abs(expected - tx.balance) <= tolerance:
            chain_matches += 1
        else:
            chain_breaks.append({
                'transaction_index': tx.index,
                'expected_balance': round(expected, 2),
                'actual_balance': tx.balance,
            })
            alerts.append(Alert(
                'chain_break', 'error',
                f'Solde incohérent : attendu {round(expected, 2)}, '
                f'lu {tx.balance} (transaction {tx.index}).',
                transaction_index=tx.index))
            tx.confidence = min(tx.confidence, 0.5)
        prev_balance = tx.balance
    chain_checked = chain_matches + len(chain_breaks)

    # règle d'ensemble
    sum_rule_ok = None
    if opening is not None and closing is not None:
        expected_closing = opening + total_credit - total_debit
        sum_rule_ok = abs(expected_closing - closing) <= tolerance
        if not sum_rule_ok:
            alerts.append(Alert(
                'unbalanced_statement', 'error',
                f'Ouverture + crédits − débits = {round(expected_closing, 2)} '
                f'≠ clôture lue {closing}.'))

    checks = {
        'tolerance': tolerance,
        'chain_checked': chain_checked,
        'chain_matches': chain_matches,
        'chain_breaks': chain_breaks,
        'chain_ok': (chain_checked > 0 and len(chain_breaks) == 0)
                    if chain_checked else None,
        'opening_closing_ok': sum_rule_ok,
        'totals': {
            'count': len(transactions),
            'debits': round(total_debit, 2),
            'credits': round(total_credit, 2),
        },
    }
    return checks, alerts


def document_confidence(transactions: list[Transaction],
                        alerts: list[Alert]) -> float:
    """Confiance document : moyenne des confiances de transactions, plafonnée
    par la présence d'alertes d'erreur. Heuristique documentée, pas une
    probabilité calibrée."""
    if not transactions:
        return 0.0
    avg = sum(t.confidence for t in transactions) / len(transactions)
    if any(a.severity == 'error' for a in alerts):
        avg = min(avg, 0.5)
    return round(max(0.0, min(1.0, avg)), 3)
