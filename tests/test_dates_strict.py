"""Tests de validation calendaire stricte des dates (v0.2.0).

Une date impossible ne doit JAMAIS être acceptée ni convertie :
31/02, 30/02, 31/04, 31/06, 29/02 non bissextile → None + alerte.
"""
import pytest

from statementflow.core import parse_date
from statementflow.engine import parse_bytes


class TestCalendrierStrict:
    @pytest.mark.parametrize('raw,expected', [
        # jour réel par mois
        ('31/01/2026', '2026-01-31'),
        ('31/04/2026', None),      # avril a 30 jours
        ('31/06/2026', None),      # juin a 30 jours
        ('31/09/2026', None),
        ('31/11/2026', None),
        ('30/04/2026', '2026-04-30'),
        # février et années bissextiles
        ('29/02/2024', '2024-02-29'),   # bissextile (divisible par 4)
        ('29/02/2000', '2000-02-29'),   # bissextile (divisible par 400)
        ('29/02/2023', None),           # non bissextile
        ('29/02/1900', None),           # divisible par 100 mais pas 400
        ('28/02/2026', '2026-02-28'),
        ('31/02/2026', None),
        ('30/02/2026', None),
        # ISO
        ('2026-02-31', None),
        ('2024-02-29', '2024-02-29'),
        ('2023-02-29', None),
        # séparateurs mélangés acceptés (même format J/M/A)
        ('31/01.2026', '2026-01-31'),
        ('31-01-2026', '2026-01-31'),
        ('31.01.2026', '2026-01-31'),
        # séparateur interne incohérent / espaces OCR : non interprétable
        ('31/02 /2026', None),
        ('31 /02/2026', None),
        ('3l/02/2026', None),      # confusion OCR l ↔ 1
        ('3l/O2/2026', None),      # confusion OCR O ↔ 0
        ('  15/03/2026  ', '2026-03-15'),  # espaces extérieurs OK
        # dates partielles / non-dates
        ('02/2026', None),
        ('2026', None),
        ('', None),
        (None, None),
        ('hello', None),
        ('99/99/2026', None),
        ('33/07/2026', None),
        # bornes d'années plausibles (relevés bancaires)
        ('01/01/1900', '1900-01-01'),
        ('31/12/2100', '2100-12-31'),
        ('31/12/1899', None),
        ('01/01/2101', None),
    ])
    def test_parse_date(self, raw, expected):
        assert parse_date(raw) == expected


class TestDatesImpossibleViaMoteur:
    """Le moteur signale, ne convertit jamais : 31/02 → date=null + alerte."""

    def _parse(self, date_str):
        data = (f'Date;Libelle;Montant;Solde\n'
                f'{date_str};LIGNE A;100;900\n').encode()
        return parse_bytes(data, 'x.csv')

    @pytest.mark.parametrize('bad', ['31/02/2026', '30/02/2026',
                                     '31/04/2026', '31/06/2026',
                                     '29/02/2023'])
    def test_impossible_null_et_alerte(self, bad):
        r = self._parse(bad)
        assert r['transactions'][0]['date'] is None
        codes = [a['code'] for a in r['alerts']]
        assert 'date_unparseable' in codes
        assert r['transactions'][0]['confidence'] < 1.0
        assert bad in r['transactions'][0]['original_description'] \
            or r['transactions'][0]['original_description'] == 'LIGNE A'

    def test_29_fevrier_bissextile_accepte(self):
        r = self._parse('29/02/2024')
        assert r['transactions'][0]['date'] == '2024-02-29'
        assert not any(a['code'] == 'date_unparseable' for a in r['alerts'])

    def test_periode_document_exclut_dates_impossibles(self):
        data = ('Date;Libelle;Montant;Solde\n'
                '01/01/2026;A;100;900\n'
                '31/02/2026;B;50;950\n'
                '15/03/2026;C;20;930\n').encode()
        r = parse_bytes(data, 'x.csv')
        assert r['document']['statement_period'] == {
            'start': '2026-01-01', 'end': '2026-03-15'}
