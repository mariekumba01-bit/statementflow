"""Tests unitaires des primitives (montants, dates, devises, directions)."""
import pytest

from statementflow.core import detect_currency, parse_amount, parse_date
from statementflow.engine import _direction_of
from statementflow.tabular import map_headers, read_tabular


class TestParseAmount:
    @pytest.mark.parametrize('raw,expected', [
        ('1 234,56', 1234.56),                    # FR espaces
        ('1\u00a0234,56', 1234.56),               # FR insécable
        ('1.234,56', 1234.56),                    # DE
        ('1,234.56', 1234.56),                    # UK
        ('1 234 567', 1234567.0),                 # XOF entier
        ('1 234 567 XOF', 1234567.0),             # suffixe devise
        ('1 234,56 EUR', 1234.56),
        ('82,40 €', 82.40),
        ('(750,38)', -750.38),                    # parenthèses comptables
        ('-1 041,01', -1041.01),                  # signe
        ('1.234,56-', -1234.56),                  # signe suffixe (ES)
        ('-1583,5', -1583.5),                     # décimale à 1 chiffre
        ('0', 0.0),
        ('27,69', 27.69),
    ])
    def test_formats(self, raw, expected):
        assert parse_amount(raw) == pytest.approx(expected), raw

    @pytest.mark.parametrize('raw', ['', None, '-', 'abc', 'N/A', '.', '--'])
    def test_non_montants(self, raw):
        assert parse_amount(raw) is None


class TestParseDate:
    @pytest.mark.parametrize('raw,expected', [
        ('31/01/2026', '2026-01-31'),
        ('01.06.2026', '2026-06-01'),             # DE
        ('01-06-2026', '2026-06-01'),             # NL
        ('2026-01-31', '2026-01-31'),             # ISO
        ('2026-01-31T10:15:00', '2026-01-31'),    # ISO datetime
        ('31/01/26', '2026-01-31'),               # année 2 chiffres
    ])
    def test_formats(self, raw, expected):
        assert parse_date(raw) == expected

    @pytest.mark.parametrize('raw', [
        '33/07/2026',   # jour impossible (défaut du générateur, reste null)
        '99/99/2026',
        'hello',
        '',
        None,
        '2026-13-45',
    ])
    def test_dates_invalides(self, raw):
        assert parse_date(raw) is None


class TestDetectCurrency:
    def test_eur(self):
        assert detect_currency('Solde 1 250,00 EUR en euros €') == 'EUR'

    def test_xof(self):
        assert detect_currency('Montant 1 234 567 XOF FCFA') == 'XOF'

    def test_aucune(self):
        assert detect_currency('Date;Libelle;Montant;Solde') is None


class TestDirection:
    @pytest.mark.parametrize('text,expected', [
        ('RECEPTION', 'credit'), ('ENVOI', 'debit'),
        ('Cash-in from agent', 'credit'), ('Cash-out at agent', 'debit'),
        ('Received from Mamadou', 'credit'),
        ('Merchant payment', None),
        ('P2P received', 'credit'),
    ])
    def test_directions(self, text, expected):
        assert _direction_of(text) == expected


class TestMapHeaders:
    def test_entete_wave(self):
        row = ['Date', 'Description', 'Sens', 'Montant (FCFA)', 'Frais', 'Solde']
        m = map_headers(row)
        assert m == {0: 'date', 1: 'label', 2: 'direction', 3: 'amount',
                     4: 'skip', 5: 'balance'}

    def test_entete_de(self):
        row = ['Datum', 'Buchungstext', 'Soll', 'Haben', 'Saldo']
        m = map_headers(row)
        assert m[0] == 'date' and m[2] == 'debit' and m[3] == 'credit' \
            and m[4] == 'balance'

    def test_suffixes_parentheses(self):
        row = ['timestamp', 'description', 'withdrawal (XOF)', 'deposit (XOF)',
               'new balance (XOF)']
        m = map_headers(row)
        assert m[2] == 'debit' and m[3] == 'credit' and m[4] == 'balance'

    def test_pas_entete(self):
        assert map_headers(['RAPPORT D ACTIVITE', 'Agent 12345']) is None


class TestReadTabular:
    def test_bom_utf8(self):
        data = '\ufeffDate;Montant\n01/02/2026;100\n'.encode('utf-8')
        rows, _ = read_tabular(data, 'x.csv')
        assert rows[0] == ['Date', 'Montant']

    def test_point_virgule(self):
        data = 'Date;Montant;Solde\n01/02/2026;100;200\n'.encode()
        rows, _ = read_tabular(data, 'x.csv')
        assert len(rows) == 2

    def test_tsv(self):
        data = 'Date\tMontant\tSolde\n2026-02-01\t100\t200\n'.encode()
        rows, _ = read_tabular(data, 'x.tsv')
        assert len(rows) == 2

    def test_xlsx_invalide(self):
        with pytest.raises(ValueError):
            read_tabular(b'PK\x03\x04 pas un zip', 'x.xlsx')
