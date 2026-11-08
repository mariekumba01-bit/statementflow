"""Contrôles de cohérence comptable et calcul de confiance.

Règles vérifiées :
- chaîne des soldes : solde_précédent + crédit − débit = solde (tolérance) ;
- règle d'ensemble : ouverture + Σcrédits − Σdébits = clôture ;
- doublons : répétition de page (identique, page différente) supprimée,
  lignes strictement identiques adjacentes signalées sans suppression ;
- ordre chronologique des dates (v0.2.0) : avertissement si non chronologique,
  non vérifiable si moins de 2 dates ;
- comptage des lignes sans date et sans montant (v0.2.0).

Précision (v0.2.0) : les sommes et chaînes sont calculées en Decimal
(conversion exacte des montants parsés) — plus d'erreurs d'accumulation
des nombres flottants sur les relevés à nombreuses lignes. Les sorties
restent des floats arrondis à 2 décimales (compatibilité v0.1).

Statut de validation (v0.2.0, quatre niveaux distincts) :
- « validated »    : contrôles passés, aucune alerte ;
- « warning »      : contrôles passés, avertissements présents ;
- « inconsistent » : au moins une alerte d'erreur (chaîne rompue,
  déséquilibre…) ;
- « unverifiable » : rien n'a pu être vérifié (aucun solde dans le
  document) — l'absence de données n'est PAS une incohérence.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .core import Alert, Transaction


def _dec(v: Optional[float]) -> Decimal:
    """float/None → Decimal exact (les montants viennent de chaînes parsées)."""
    return Decimal(str(0.0 if v is None else v))


def _f(d: Decimal) -> float:
    """Decimal → float arrondi à 2 décimales (format de sortie v0.1)."""
    return float(d.quantize(Decimal('0.01')))


def amounts_equal(a: Optional[float], b: Optional[float], tol: float = 0.01) -> bool:
    """Égalité de montants avec tolérance (calcul Decimal).

    None ≡ 0 uniquement quand l'AUTRE valeur est aussi nulle (à ± tol) :
    « None » signifie « colonne vide », écrite « 0 » dans de nombreux
    exports — mais None ne doit jamais masquer un montant non nul.
    """
    av, bv = _dec(a), _dec(b)
    if a is None or b is None:
        return abs(av - bv) <= Decimal(str(tol)) \
            and abs(av) <= Decimal(str(tol)) and abs(bv) <= Decimal(str(tol))
    return abs(av - bv) <= Decimal(str(tol))


def deduplicate(transactions: list[Transaction],
                ) -> tuple[list[Transaction], list[Alert], dict]:
    """Supprime les répétitions de page, signale les doublons adjacents.

    Une « répétition de page » est une transaction strictement identique
    (date, libellé, montants, solde) à la précédente mais extraite d'une
    autre page : certains relevés réimpriment la dernière ligne en haut de
    la page suivante. Les doublons au sein d'une même page sont conservés
    (cas légitime en comptabilité) mais signalés.

    Retourne (kept, alerts, index_map) : index_map fait correspondre
    l'index ORIGINAL de chaque transaction à son index APRÈS renumérotation
    (les répétitions supprimées pointent vers l'originale conservée) —
    le moteur remappe ainsi les alertes de parsing émises avant dédup.
    """
    kept: list[Transaction] = []
    alerts: list[Alert] = []
    removed_alerts: list[tuple[int, Alert]] = []  # (index original, alerte)
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
                removed_alerts.append((tx.index, Alert(
                    'duplicate_page_repeat', 'info',
                    f'Transaction identique à la précédente répétée en haut de la '
                    f'page {tx.page} (originale page {prev.page}) conservée une seule fois.',
                    transaction_index=None)))
                continue
            if same:
                removed_alerts.append((tx.index, Alert(
                    'possible_duplicate', 'warning',
                    'Deux transactions strictement identiques et adjacentes '
                    '(conservées toutes les deux : cas légitime possible).',
                    transaction_index=None)))
        kept.append(tx)
    # renumérotation puis correspondance original → nouveau
    index_map: dict[int, int] = {}
    for i, tx in enumerate(kept, 1):
        index_map[tx.index] = i
        tx.index = i
    for orig_index, alert in removed_alerts:
        # la répétition supprimée renvoie vers l'originale conservée
        prev_orig = orig_index - 1
        while prev_orig not in index_map and prev_orig > 0:
            prev_orig -= 1
        alert.transaction_index = index_map.get(prev_orig)
        alerts.append(alert)
    return kept, alerts, index_map


def compute_checks(transactions: list[Transaction],
                   opening: Optional[float],
                   closing: Optional[float],
                   tolerance: float = 0.01):
    """Contrôles de cohérence → (checks, alerts). Calculs en Decimal."""
    alerts: list[Alert] = []
    tol = Decimal(str(tolerance))
    total_debit = sum((_dec(t.debit) for t in transactions), Decimal(0))
    total_credit = sum((_dec(t.credit) for t in transactions), Decimal(0))

    # chaîne des soldes
    chain_matches = 0
    chain_breaks = []
    prev_balance: Optional[Decimal] = _dec(opening) if opening is not None else None
    for tx in transactions:
        if tx.balance is None:
            continue
        bal = _dec(tx.balance)
        if prev_balance is None:
            prev_balance = bal
            continue
        expected = prev_balance + _dec(tx.credit) - _dec(tx.debit)
        if abs(expected - bal) <= tol:
            chain_matches += 1
        else:
            chain_breaks.append({
                'transaction_index': tx.index,
                'expected_balance': _f(expected),
                'actual_balance': tx.balance,
            })
            alerts.append(Alert(
                'chain_break', 'error',
                f'Solde incohérent : attendu {_f(expected)}, '
                f'lu {tx.balance} (transaction {tx.index}).',
                transaction_index=tx.index))
            tx.confidence = min(tx.confidence, 0.5)
        prev_balance = bal
    chain_checked = chain_matches + len(chain_breaks)

    # règle d'ensemble
    sum_rule_ok = None
    if opening is not None and closing is not None:
        expected_closing = _dec(opening) + total_credit - total_debit
        sum_rule_ok = abs(expected_closing - _dec(closing)) <= tol
        if not sum_rule_ok:
            alerts.append(Alert(
                'unbalanced_statement', 'error',
                f'Ouverture + crédits − débits = {_f(expected_closing)} '
                f'≠ clôture lue {closing}.'))

    # ordre chronologique (v0.2.0) : avertissement, jamais une erreur —
    # des dates triées par valeur (non par opération) existent dans la réalité
    date_order_ok: Optional[bool] = None
    dated = [t for t in transactions if t.date]
    for prev_tx, tx in zip(dated, dated[1:]):
        if tx.date < prev_tx.date:
            if date_order_ok is None:
                date_order_ok = False
            alerts.append(Alert(
                'date_order_inconsistent', 'warning',
                f'Dates non chronologiques : {tx.date} après {prev_tx.date} '
                f'(transaction {tx.index}). Vérifiez ou signalez au moteur.',
                transaction_index=tx.index))
    if date_order_ok is None and len(dated) >= 2:
        date_order_ok = True

    # comptages d'incomplétude (v0.2.0) — information, pas d'alerte
    # redondante : les alertes par transaction existent déjà
    without_date = sum(1 for t in transactions if t.date is None)
    without_amount = sum(1 for t in transactions
                         if t.debit is None and t.credit is None)

    checks = {
        'tolerance': tolerance,
        'chain_checked': chain_checked,
        'chain_matches': chain_matches,
        'chain_breaks': chain_breaks,
        'chain_ok': (chain_checked > 0 and len(chain_breaks) == 0)
                    if chain_checked else None,
        'opening_closing_ok': sum_rule_ok,
        'date_order_ok': date_order_ok,
        'transactions_without_date': without_date,
        'transactions_without_amount': without_amount,
        'totals': {
            'count': len(transactions),
            'debits': _f(total_debit),
            'credits': _f(total_credit),
        },
    }
    return checks, alerts


def validation_status(checks: dict, alerts: list[Alert]) -> dict:
    """Statut de validation du document, quatre niveaux distincts.

    - inconsistent : alerte d'erreur (chaîne rompue, déséquilibre) ;
    - unverifiable : aucun contrôle possible (aucun solde disponible) —
      l'absence de données n'est pas déclarée comme une incohérence ;
    - warning : contrôles passés mais avertissements (date manquante,
      sens supposé, OCR faible…) ;
    - validated : contrôles passés, aucune alerte.
    """
    has_error = any(a.severity == 'error' for a in alerts)
    has_warning = any(a.severity == 'warning' for a in alerts)
    verifiable = bool(checks.get('chain_checked')) \
        or checks.get('opening_closing_ok') is not None
    if has_error:
        status = 'inconsistent'
    elif not verifiable:
        status = 'unverifiable'
    elif has_warning:
        status = 'warning'
    else:
        status = 'validated'
    return {
        'status': status,
        'verifiable': verifiable,
        'errors': sum(1 for a in alerts if a.severity == 'error'),
        'warnings': sum(1 for a in alerts if a.severity == 'warning'),
    }


def document_confidence(transactions: list[Transaction],
                        alerts: list[Alert]) -> float:
    """Confiance document : moyenne des confiances de transactions, plafonnée
    par la présence d'alertes d'erreur. Heuristique documentée, pas une
    probabilité calibrée — règle : base = moyenne des confiances par
    transaction (texte natif 1.0, OCR 0.75, OCR faible 0.6 ; −0.25 par
    donnée manquante : date, montant), plafonnée à 0.5 si alerte d'erreur.
    """
    if not transactions:
        return 0.0
    avg = sum(t.confidence for t in transactions) / len(transactions)
    if any(a.severity == 'error' for a in alerts):
        avg = min(avg, 0.5)
    return round(max(0.0, min(1.0, avg)), 3)
