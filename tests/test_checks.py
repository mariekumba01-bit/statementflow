"""Tests des contrôles de cohérence comptable et des doublons."""
from statementflow.checks import (amounts_equal, compute_checks,
                                  deduplicate, document_confidence)
from statementflow.core import Alert, Transaction


def tx(i=1, date='2026-01-01', label='L', debit=None, credit=None, balance=None,
       page=1):
    return Transaction(index=i, date=date, label=label, debit=debit,
                       credit=credit, balance=balance, currency='EUR',
                       confidence=1.0, page=page)


class TestChain:
    def test_chaine_coherente(self):
        txs = [tx(1, debit=100, balance=900), tx(2, credit=50, balance=950)]
        checks, alerts = compute_checks(txs, opening=1000, closing=950)
        assert checks['chain_matches'] == 2
        assert checks['chain_ok'] is True
        assert checks['opening_closing_ok'] is True
        assert not [a for a in alerts if a.severity == 'error']

    def test_chaine_rompue(self):
        txs = [tx(1, debit=100, balance=850)]  # attendu 900
        checks, alerts = compute_checks(txs, opening=1000, closing=850)
        assert checks['chain_breaks'][0]['expected_balance'] == 900.0
        assert checks['chain_ok'] is False
        assert any(a.code == 'chain_break' for a in alerts)

    def test_regle_ensemble(self):
        txs = [tx(1, debit=100), tx(2, credit=50)]
        checks, _ = compute_checks(txs, opening=1000, closing=949)  # attendu 950
        assert checks['opening_closing_ok'] is False

    def test_sans_soldes(self):
        txs = [tx(1, debit=100)]
        checks, _ = compute_checks(txs, opening=None, closing=None)
        assert checks['chain_checked'] == 0
        assert checks['chain_ok'] is None
        assert checks['opening_closing_ok'] is None

    def test_none_equivaut_zero(self):
        # export avec « 0 » dans la colonne crédit alors que le GT dit null
        assert amounts_equal(None, 0.0)
        assert amounts_equal(0.0, None)
        assert amounts_equal(None, None)
        assert not amounts_equal(0.0, 0.02)   # écart > tolérance
        assert not amounts_equal(None, 0.02)  # None ne masque pas un montant
        assert not amounts_equal(0.02, None)


class TestDeduplicate:
    def test_repetition_page(self):
        t1 = tx(1, label='A', debit=10, balance=90)
        t2 = tx(2, label='A', debit=10, balance=90, page=2)  # réimprimée
        t3 = tx(3, label='B', credit=5, balance=95)
        kept, alerts, index_map = deduplicate([t1, t2, t3])
        assert len(kept) == 2
        assert any(a.code == 'duplicate_page_repeat' for a in alerts)
        assert kept[-1].index == 2  # renuméroté

    def test_doublon_meme_page_conserve(self):
        t1 = tx(1, label='A', debit=10)
        t2 = tx(2, label='A', debit=10)
        kept, alerts, index_map = deduplicate([t1, t2])
        assert len(kept) == 2
        assert any(a.code == 'possible_duplicate' for a in alerts)


class TestConfiance:
    def test_plafonnee_par_erreur(self):
        txs = [tx(1, debit=10)]  # confidence=1.0 par défaut dans le helper
        alerts = [Alert('chain_break', 'error', 'x')]
        assert document_confidence(txs, alerts) <= 0.5

    def test_sans_transactions(self):
        assert document_confidence([], []) == 0.0
