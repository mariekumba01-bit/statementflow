"""Tests pytest de la CLI (subprocess réels)."""
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, 'data', 'corpus')
DOC = os.path.join(CORPUS, 'SYNTH_fr_societegenerale_A.pdf')


def run_cli(*args):
    return subprocess.run(
        [sys.executable, '-m', 'statementflow.cli', *args],
        capture_output=True, text=True, cwd=os.path.dirname(HERE))


class TestParse:
    def test_parse_fichier_stdout(self):
        r = run_cli('parse', DOC, '--pretty')
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert len(data['transactions']) == 14

    def test_parse_output(self, tmp_path):
        out = tmp_path / 'result.json'
        r = run_cli('parse', DOC, '-o', str(out))
        assert r.returncode == 0, r.stderr
        data = json.loads(out.read_text(encoding='utf-8'))
        assert data['document']['kind'] == 'pdf'
        assert 'contrôles' in r.stderr  # résumé sur stderr

    def test_parse_dossier_outdir(self, tmp_path):
        outdir = tmp_path / 'out'
        r = run_cli('parse', CORPUS, '--outdir', str(outdir), '--quiet')
        assert r.returncode == 0, r.stderr
        # 32 documents + 3 fichiers auxiliaires (ground_truth, inventaire…)
        jsons = list(outdir.glob('*.json'))
        assert len(jsons) >= 32

    def test_currency_hint(self, tmp_path):
        out = tmp_path / 'r.json'
        doc = os.path.join(CORPUS, 'SYNTH_csv_orange_money_V.csv')
        r = run_cli('parse', doc, '-o', str(out), '--quiet',
                    '--currency-hint', 'XOF')
        data = json.loads(out.read_text(encoding='utf-8'))
        assert data['currency'] == 'XOF'

    def test_fichier_inexistant(self):
        r = run_cli('parse', '/tmp/nexiste_pas.pdf')
        assert r.returncode == 1

    def test_version(self):
        r = run_cli('--version')
        assert r.returncode == 0


class TestCheck:
    def test_check_ok(self, tmp_path):
        out = tmp_path / 'r.json'
        run_cli('parse', DOC, '-o', str(out), '--quiet')
        r = run_cli('check', str(out))
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data['checks']['opening_closing_ok'] is True

    def test_check_desequilibre(self, tmp_path):
        out = tmp_path / 'r.json'
        run_cli('parse',
                os.path.join(CORPUS, 'SYNTH_stress_scanned_T.pdf'),
                '-o', str(out), '--quiet')
        r = run_cli('check', str(out))
        assert r.returncode == 2  # document déséquilibré (détecté)
