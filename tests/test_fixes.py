"""Tests de non-régression des quatre correctifs du moteur v3.

1. entêtes « (suite) » de pages suivantes ignorées ;
2. lignes avec dates impossibles conservées avec date=null + alerte ;
3. OCR : fusion des fragments de milliers + clustering top-k ;
4. devise jamais inventée : null + alerte, sauf currency_hint explicite.
"""
import json
import os

import pytest

from statementflow import parse_file
from statementflow.engine import ParseError

CORPUS = os.path.join(os.path.dirname(__file__), 'data', 'corpus')


def parse(name, **kw):
    return parse_file(os.path.join(CORPUS, name), **kw)


class TestFix1EnteteSuite:
    def test_boa_multipage(self):
        """Avant : « BANQUE DE L AFRIQUE DE L OUEST - RELEVE (suite) »
        fusionné dans un libellé. Après : entête ignorée + alerte."""
        r = parse('SYNTH_uemoa_boa_sn_P.pdf')
        pollues = [t for t in r['transactions']
                   if 'BANQUE DE L' in t['label'].upper()
                   or 'RELEVE DE COMPTE' in t['label'].upper()]
        assert not pollues
        assert any(a['code'] == 'page_header_skipped' for a in r['alerts'])


class TestFix2DatesInvalides:
    def test_multipage_40tx(self):
        """Le document contient 9 dates « 33/07 » à « 40/07 » (défaut du
        générateur) : elles sont conservées avec date=null et signalées,
        pas perdues ni inventées."""
        r = parse('SYNTH_stress_multipage_40tx_R.pdf')
        assert len(r['transactions']) == 40  # aucune transaction perdue
        nulls = [t for t in r['transactions'] if t['date'] is None]
        assert len(nulls) == 9
        assert sum(1 for a in r['alerts'] if a['code'] == 'date_unparseable') == 9
        for t in nulls:
            assert t['confidence'] < 1.0  # confiance abaissée

    def test_date_impossible_csv(self):
        from statementflow.engine import parse_bytes
        data = ('Date;Description;Montant;Solde\n'
                '33/07/2026;LIGNE A;100;900\n').encode()
        r = parse_bytes(data, 'x.csv')
        assert r['transactions'][0]['date'] is None
        assert any(a['code'] == 'date_unparseable' for a in r['alerts'])


class TestFix3OCR:
    def test_scanne(self):
        """Document scanné (image) : OCR → 12 transactions complètes avec
        débit/crédit corrects (avant : colonnes confondues)."""
        r = parse('SYNTH_stress_scanned_T.pdf')
        assert r['document']['method'] == 'ocr'
        assert any(a['code'] == 'ocr_used' for a in r['alerts'])
        assert len(r['transactions']) == 12
        with open(os.path.join(CORPUS, 'ground_truth.json'), encoding='utf-8') as f:
            gt = json.load(f)['SYNTH_stress_scanned_T.pdf']['transactions']
        ok = 0
        for p, g in zip(r['transactions'], gt):
            if p['date'] == g['date'] and (p['debit'] or 0) == (g['debit'] or 0) \
                    and (p['credit'] or 0) == (g['credit'] or 0):
                ok += 1
        assert ok >= 11  # 11/12 minimum (tolérance OCR sur 1 ligne possible)
        assert r['confidence'] <= 0.6  # confiance OCR abaissée

    def test_scanne_fragments_milliers(self):
        """« 3 105,54 » scanné est lu « 3 » + « 105,54 » : fusionné (8 pt)."""
        r = parse('SYNTH_stress_scanned_T.pdf')
        balances = [t['balance'] for t in r['transactions'] if t['balance']]
        # le solde 3105.54 doit exister (pas 105.54 ni 3.0)
        assert any(abs(b - 3105.54) < 0.01 for b in balances)


class TestFix4Devise:
    def test_csv_sans_devise(self):
        """Orange Money CSV : aucun jeton de devise → null + alerte."""
        r = parse('SYNTH_csv_orange_money_V.csv')
        assert r['currency'] is None
        assert any(a['code'] == 'currency_not_identified' for a in r['alerts'])

    def test_currency_hint(self):
        r = parse('SYNTH_csv_orange_money_V.csv', currency_hint='XOF')
        assert r['currency'] == 'XOF'
        assert not any(a['code'] == 'currency_not_identified'
                       for a in r['alerts'])

    def test_devise_detectee_xof(self):
        r = parse('SYNTH_csv_xof_suffix_AF.csv')
        assert r['currency'] == 'XOF'  # lisible dans le document


class TestComportementsHonnêtes:
    def test_incoherence_reelle_du_document_detectee(self):
        """Le document scanné imprime une ouverture incohérente avec sa
        propre chaîne de soldes : le moteur le SIGNALE (c'est son rôle)."""
        r = parse('SYNTH_stress_scanned_T.pdf')
        assert r['checks']['opening_closing_ok'] is False
        assert any(a['code'] in ('unbalanced_statement', 'chain_break')
                   for a in r['alerts'])

    def test_pdf_corrompu_erreur_comprehensible(self):
        with pytest.raises(ParseError, match='illisible ou corrompu'):
            parse_bytes_ou_erreur()

    def test_extension_mensongere(self):
        from statementflow.engine import parse_bytes
        with pytest.raises(ParseError):
            parse_bytes(b'\x89PNG\r\n\x1a\n', 'photo.jpg')


def parse_bytes_ou_erreur():
    from statementflow.engine import parse_bytes
    return parse_bytes(b'%PDF-pas-un-vrai-pdf', 'fake.pdf')
