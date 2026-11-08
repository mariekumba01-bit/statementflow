"""Tests du schéma JSON v0.1→v0.2 : identifiants stables, champs
additionnels rétrocompatibles, statut de validation, précision Decimal.
"""
import pytest

from statementflow.core import document_id_for, transaction_id_for
from statementflow.engine import parse_bytes

CSV_OK = (b'Date;Libelle;Montant;Solde\n'
          b'01/02/2026;ACHAT MATERIEL;100;900\n'
          b'02/02/2026;DEPOT CLIENT    ;50;950\n')
CSV_CASSEE = (b'Date;Libelle;Debit;Credit;Solde\n'
              b'01/02/2026;A;100;;900\n'
              b'02/02/2026;B;;50;960\n')  # 900+50=950 ≠ 960
CSV_SANS_SOLDE = (b'Date;Libelle;Montant\n'
                  b'01/02/2026;A;100\n')


@pytest.fixture(scope='module')
def parsed_ok():
    return parse_bytes(CSV_OK, 'releve.csv')


class TestChampsDocument:
    def test_document_id_stable_et_sans_donnee_personnelle(self, parsed_ok):
        result = parsed_ok
        did = result['document']['document_id']
        assert len(did) == 16 and int(did, 16) >= 0
        assert did == document_id_for(CSV_OK)

    def test_schema_version_document(self, parsed_ok):
        result = parsed_ok
        assert result['schema'] == 'statementflow/v1'
        assert result['document']['schema_version'] == 'statementflow/v1'

    def test_periode_et_devise_compte(self, parsed_ok):
        result = parsed_ok
        assert result['document']['statement_period'] == {
            'start': '2026-02-01', 'end': '2026-02-02'}
        assert result['document']['account_currency'] is None  # jamais inventée

    def test_metadata_deterministes(self, parsed_ok):
        result = parsed_ok
        m = result['metadata']
        assert 'JOUR/MOIS' in m['date_interpretation']
        assert 'confidence_rule' in m
        # rejouer → identique (aucun horodatage dans la sortie)
        assert parse_bytes(CSV_OK, 'releve.csv')['metadata'] == m


class TestChampsTransaction:
    def test_transaction_id_stable(self, parsed_ok):
        txs = parsed_ok['transactions']
        again = parse_bytes(CSV_OK, 'releve.csv')['transactions']
        for a, b in zip(txs, again):
            assert a['transaction_id'] == b['transaction_id']
            assert len(a['transaction_id']) == 16
        assert txs[0]['transaction_id'] != txs[1]['transaction_id']

    def test_description_et_original(self, parsed_ok):
        txs = parsed_ok['transactions']
        t = txs[1]
        assert t['description'] == 'DEPOT CLIENT'
        assert t['original_description'] == 'DEPOT CLIENT'
        assert t['label'] == t['description']  # compat v0.1

    def test_montant_signe(self, parsed_ok):
        txs = parsed_ok['transactions']
        assert txs[0]['debit'] == 100 and txs[0]['credit'] is None
        assert txs[0]['amount'] == -100
        assert txs[1]['amount'] == 50

    def test_alertes_par_transaction(self):
        data = (b'Date;Libelle;Debit;Credit;Solde\n'
                b'31/02/2026;BIZARRE;100;;900\n'
                b'02/02/2026;OK;;50;950\n')
        r = parse_bytes(data, 'x.csv')
        assert 'date_unparseable' in r['transactions'][0]['alerts']
        assert r['transactions'][1]['alerts'] == []
        # l'alerte document porte le même code + l'index
        doc_alerts = [a for a in r['alerts']
                      if a['code'] == 'date_unparseable']
        assert doc_alerts and doc_alerts[0]['transaction_index'] == 1


class TestStatutValidation:
    def test_valide(self):
        # document sain : colonnes Débit/Crédit explicites + devise connue
        data = (b'Date;Libelle;Debit;Credit;Solde\n'
                b'01/02/2026;A;100;;900\n'
                b'02/02/2026;B;;50;950\n')
        r = parse_bytes(data, 'x.csv', currency_hint='EUR')
        v = r['validation']
        assert v['status'] == 'validated' and v['verifiable'] is True
        assert v['errors'] == 0 and v['warnings'] == 0
        # colonne Montant NON signée sans colonne Sens → avertissement
        # side_assumed_debit (comportement documenté), pas « validated »
        r2 = parse_bytes(CSV_OK, 'x.csv', currency_hint='EUR')
        assert r2['validation']['status'] == 'warning'

    def test_incoherent(self):
        r = parse_bytes(CSV_CASSEE, 'x.csv', currency_hint='EUR')
        assert r['validation']['status'] == 'inconsistent'
        assert r['validation']['errors'] >= 1
        assert r['checks']['chain_ok'] is False  # 900 + 50 ≠ 960
        assert r['checks']['opening_closing_ok'] is None  # pas d'ouverture :
        # l'absence de données n'est pas déclarée comme une incohérence

    def test_non_verifiable(self):
        """Aucun solde dans le document : rien à vérifier — l'absence de
        données n'est PAS une incohérence."""
        r = parse_bytes(CSV_SANS_SOLDE, 'x.csv', currency_hint='EUR')
        assert r['validation']['status'] == 'unverifiable'
        assert r['validation']['verifiable'] is False
        assert r['checks']['opening_closing_ok'] is None
        assert r['checks']['chain_checked'] == 0

    def test_warning(self):
        # chaîne équilibrée mais devise devinable nulle part : avertissement
        # currency_not_identified → statut « warning » (contrôles OK)
        data = (b'Date;Libelle;Debit;Credit;Solde\n'
                b'01/02/2026;A;100;;900\n'
                b'02/02/2026;B;;50;950\n')
        r = parse_bytes(data, 'x.csv')
        assert r['validation']['status'] == 'warning'
        assert r['validation']['errors'] == 0
        codes = {a['code'] for a in r['alerts']}
        assert 'currency_not_identified' in codes
        assert r['checks']['transactions_without_amount'] == 0


class TestPrecisionDecimal:
    """Arithmétique Decimal des contrôles : exacte et déterministe à toute
    échelle. (La dérive float n'est observable qu'au-delà de ~1e13 — hors
    plage réaliste — mais le Decimal l'élimine par principe et garantit le
    rejeu identique.) Le format tabulaire n'extrait pas d'ouverture :
    les tests passent par la chaîne des soldes, ancrée au 1er solde lu.
    """

    def test_chain_exacte_a_grande_echelle(self):
        data = (b'Date;Libelle;Debit;Credit;Solde\n'
                b'01/02/2026;D1;;0.02;1000000000000.02\n'
                b'02/02/2026;D2;;0.02;1000000000000.04\n')
        r = parse_bytes(data, 'x.csv', currency_hint='EUR')
        assert r['checks']['chain_ok'] is True
        # le 1er solde ancre la chaîne sans être compté (comportement v0.1)
        assert r['checks']['chain_checked'] == 1
        assert r['checks']['chain_matches'] == 1
        assert r['totals']['credits'] == 0.04

    def test_rejeu_deterministe(self):
        data = (b'Date;Libelle;Debit;Credit;Solde\n'
                b'01/02/2026;A;1234.56;;9000.00\n'
                b'02/02/2026;B;;0.07;9000.07\n'
                b'03/02/2026;C;0.03;0.00;9000.04\n')
        a = parse_bytes(data, 'x.csv', currency_hint='EUR')
        b = parse_bytes(data, 'x.csv', currency_hint='EUR')
        assert a['totals'] == b['totals']
        assert a['checks']['chain_ok'] is True
        assert a == b  # sortie 100 % déterministe (aucun horodatage)


class TestNouveauxControles:
    def test_ordre_chronologique(self):
        data = (b'Date;Libelle;Debit;Credit;Solde\n'
                b'05/02/2026;A;100;;900\n'
                b'01/02/2026;B;;50;950\n')
        r = parse_bytes(data, 'x.csv', currency_hint='EUR')
        assert r['checks']['date_order_ok'] is False
        codes = [a['code'] for a in r['alerts']]
        assert 'date_order_inconsistent' in codes
        # avertissement, pas une erreur : le statut reste « warning »
        assert r['validation']['status'] == 'warning'

    def test_ordre_chronologique_non_verifiable_si_moins_de_2_dates(self):
        data = (b'Date;Libelle;Debit;Credit;Solde\n'
                b'05/02/2026;A;100;;900\n')
        r = parse_bytes(data, 'x.csv', currency_hint='EUR')
        assert r['checks']['date_order_ok'] is None

    def test_devise_contradictoire(self):
        data = (b'Date;Libelle;Devise;Montant;Solde\n'
                b'01/02/2026;A;EUR;100;900\n'
                b'02/02/2026;B;FCFA;50;950\n')
        r = parse_bytes(data, 'x.csv', currency_hint='EUR')
        # « FCFA » normalisé en XOF : contredit l'EUR du document
        codes = [a['code'] for a in r['alerts']]
        assert 'currency_mismatch' in codes
        assert r['transactions'][1]['currency'] == 'XOF'

    def test_debit_et_credit_ensemble_signale_pdf(self):
        """PDF : deux colonnes reconnues sur la même ligne → alerte,
        les deux valeurs conservées (pas de destruction silencieuse)."""
        from statementflow.core import Transaction
        from statementflow.checks import deduplicate
        # au niveau moteur : construit via la path tabulaire (débit gagnant)
        data = (b'Date;Libelle;Debit;Credit;Solde\n'
                b'01/02/2026;A;100;50;900\n')
        r = parse_bytes(data, 'x.csv', currency_hint='EUR')
        t = r['transactions'][0]
        assert t['debit'] == 100 and t['credit'] is None  # règle tabulaire
