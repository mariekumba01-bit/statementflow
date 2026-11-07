"""Tests des fichiers malformés et entrées hostiles : erreurs propres,
jamais de crash, jamais de donnée inventée."""
import io
import os

import pytest

from statementflow import ParseError, parse_bytes
from statementflow.engine import detect_kind


class TestDetectKind:
    def test_pdf(self):
        assert detect_kind('a.pdf', b'%PDF-1.7 whatever') == 'pdf'

    def test_pdf_mensonger(self):
        with pytest.raises(ParseError, match='n\'est pas un PDF'):
            detect_kind('a.pdf', b'plain text pretending')

    def test_xlsx(self):
        assert detect_kind('a.xlsx', b'PK\x03\x04zip') == 'xlsx'

    def test_csv(self):
        assert detect_kind('a.csv', b'a;b;c') == 'csv'

    def test_inconnu(self):
        """Binaire non texte avec extension inconnue : erreur explicite."""
        with pytest.raises(ParseError):
            detect_kind('a.docx', b'\xff\xfe\x00\x01\x02')


class TestMalformes:
    def test_vide(self):
        with pytest.raises(ParseError, match='vide'):
            parse_bytes(b'', 'x.pdf')

    def test_pdf_corrompu(self):
        with pytest.raises(ParseError, match='illisible ou corrompu'):
            parse_bytes(b'%PDF-broken', 'x.pdf')

    def test_csv_sans_entete_ni_dates(self):
        """CSV lisible mais sans structure → 0 transactions, pas de crash."""
        r = parse_bytes(b'name,age\nalice,30\nbob,25\n', 'people.csv')
        assert r['transactions'] == []

    def test_csv_entete_seule(self):
        r = parse_bytes(b'Date;Libelle;Montant;Solde\n', 'x.csv')
        assert r['transactions'] == []

    def test_csv_lignes_incompletes(self):
        data = ('Date;Libelle;Montant;Solde\n'
                '01/02/2026;COMPLETE;100;900\n'
                '02/02/2026;;\n'          # ligne vide partielle
                '03/02/2026;SANS_MONTANT;;\n').encode()
        r = parse_bytes(data, 'x.csv')
        assert len(r['transactions']) >= 2
        # la ligne sans montant est conservée et signalée
        sans_montant = [t for t in r['transactions'] if t['label'] == 'SANS_MONTANT']
        assert sans_montant and sans_montant[0]['debit'] is None
        assert any(a['code'] == 'amount_missing' for a in r['alerts'])

    def test_csv_separateurs_mixtes(self):
        """Ligne avec colonnes supplémentaires inconnues : pas de crash."""
        data = ('Date;Libelle;Montant;Solde\n'
                '01/02/2026;A;100;900;extra;colonnes\n').encode()
        r = parse_bytes(data, 'x.csv')
        assert len(r['transactions']) == 1

    def test_null_bytes(self):
        """Octets nuls : décodés en latin-1, aucune entête → 0 transactions,
        pas de crash (comportement sûr)."""
        r = parse_bytes(b'\x00\x00\x00\x00', 'x.csv')
        assert r['transactions'] == []

    def test_trop_gros(self):
        from statementflow.config import Config
        cfg = Config(max_file_mb=0.000001)
        with pytest.raises(ParseError, match='limite'):
            parse_bytes(b'x' * 10, 'x.csv', cfg)


class TestHonestete:
    def test_devise_jamais_inventee(self):
        data = ('Date;Libelle;Montant;Solde\n'
                '01/02/2026;A;100;900\n').encode()
        r = parse_bytes(data, 'x.csv')
        assert r['currency'] is None
        assert any(a['code'] == 'currency_not_identified' for a in r['alerts'])

    def test_date_impossible_signalée(self):
        data = ('Date;Libelle;Montant;Solde\n'
                '32/13/2026;IMPOSSIBLE;100;900\n').encode()
        r = parse_bytes(data, 'x.csv')
        assert r['transactions'][0]['date'] is None
        assert any(a['code'] == 'date_unparseable' for a in r['alerts'])

    def test_confiance_baisse_sur_alertes(self):
        data = ('Date;Libelle;Montant;Solde\n'
                '32/13/2026;IMPOSSIBLE;100;900\n').encode()
        r = parse_bytes(data, 'x.csv')
        assert r['transactions'][0]['confidence'] < 1.0

    def test_trace_presente(self):
        data = ('Date;Libelle;Montant;Solde\n'
                '01/02/2026;A;100;900\n').encode()
        r = parse_bytes(data, 'x.csv')
        t = r['transactions'][0]
        assert 'trace' in t and t['trace']['method'] == 'csv-row'
        assert t['trace']['source_row'] == 2
