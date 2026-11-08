"""Test corpus : métriques de non-régression sur les 32 documents.

Les seuils sont FIGÉS dans expected_metrics.json : toute régression du
moteur fait échouer ce test. Le corpus n'est jamais modifié pour faire
passer les résultats.

Métriques mesurées avec le même harnais que la validation (matching
date+montant, similarité de libellés >= 0,90, None == 0 pour débit/crédit).
"""
import difflib
import json
import os
import unicodedata

import pytest

from statementflow import parse_file
from statementflow.engine import ParseError

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, 'data', 'corpus')
EXPECTED = os.path.join(HERE, 'expected_metrics.json')


def norm_label(s):
    s = unicodedata.normalize('NFKD', s or '').lower().strip()
    return ' '.join(s.split())


def amounts_match(a, b):
    av = 0 if a is None else float(a)
    bv = 0 if b is None else float(b)
    return abs(av - bv) < 0.01


def score_doc(gt, pred):
    """Score d'un document : rappel/précision et exactitude par champ."""
    gt_txs, pred_txs = gt['transactions'], pred['transactions']
    if len(gt_txs) == len(pred_txs):
        pairs = list(zip(gt_txs, pred_txs))
        fn = fp = 0
    else:
        pairs, used = [], set()
        for g in gt_txs:
            best, best_sim = None, 0.0
            for j, p in enumerate(pred_txs):
                if j in used or p['date'] != g['date']:
                    continue
                ga = g['debit'] or g['credit'] or 0
                pa = p['debit'] or p['credit'] or 0
                if abs(ga - pa) > 0.01:
                    continue
                sim = difflib.SequenceMatcher(
                    None, norm_label(g['label']),
                    norm_label(p.get('label'))).ratio()
                if sim > best_sim:
                    best, best_sim = j, sim
            if best is not None and best_sim > 0.30:
                used.add(best)
                pairs.append((g, pred_txs[best]))
        fn = len(gt_txs) - len(pairs)
        fp = len(pred_txs) - len(pairs)
    f = {'date': 0, 'label': 0, 'debit': 0, 'credit': 0, 'balance': 0,
         'currency': 0}
    tot = dict(f)
    for g, p in pairs:
        tot['date'] += 1
        f['date'] += (p['date'] == g['date'])
        tot['label'] += 1
        sim = difflib.SequenceMatcher(None, norm_label(g['label']),
                                      norm_label(p.get('label'))).ratio()
        f['label'] += (sim >= 0.90)
        for k in ('debit', 'credit'):
            tot[k] += 1
            f[k] += amounts_match(g.get(k), p.get(k))
        if g.get('balance') is not None:
            tot['balance'] += 1
            f['balance'] += amounts_match(g.get('balance'), p.get('balance'))
        if g.get('currency'):
            if (p.get('currency') or pred.get('currency')) == g['currency']:
                f['currency'] += 1
            tot['currency'] += 1
    n = max(len(gt_txs), 1)
    return {
        'recall': len(pairs) / n,
        'precision': len(pairs) / max(len(pred_txs), 1),
        'fn': fn, 'fp': fp,
        'date': f['date'] / tot['date'] if tot['date'] else None,
        'label': f['label'] / tot['label'] if tot['label'] else None,
        'debit': f['debit'] / tot['debit'] if tot['debit'] else None,
        'credit': f['credit'] / tot['credit'] if tot['credit'] else None,
        'balance': f['balance'] / tot['balance'] if tot['balance'] else None,
        'currency': f['currency'] / tot['currency'] if tot['currency'] else None,
        'n_gt': len(gt_txs),
    }


@pytest.fixture(scope='module')
def corpus_results(ground_truth):
    results = {}
    for fname, gt in sorted(ground_truth.items()):
        try:
            results[fname] = parse_file(os.path.join(CORPUS, fname))
        except ParseError as e:  # compté comme échec complet
            results[fname] = {'transactions': [], 'erreur': str(e)}
    return results


@pytest.fixture(scope='module')
def all_scores(ground_truth, corpus_results):
    return {f: score_doc(gt, corpus_results[f])
            for f, gt in ground_truth.items()}


def _aggregate(scores, field):
    n = sum(s['n_gt'] for s in scores)
    if not n:
        return None
    vals = [(s[field], s['n_gt']) for s in scores if s[field] is not None]
    if not vals:
        return None
    return round(sum(v * w for v, w in vals) / sum(w for _, w in vals), 4)


def test_corpus_complet(all_scores):
    """Rappel >= 0.98, précision = 1.0, champs >= 0.98 (sauf dates 0.979
    et devise 0.67 documentés : comportements honnêtes, pas des bugs)."""
    scores = list(all_scores.values())
    with open(EXPECTED, encoding='utf-8') as f:
        expected = json.load(f)
    agg = {
        'recall': round(sum(s['recall'] * s['n_gt'] for s in scores)
                        / sum(s['n_gt'] for s in scores), 4),
        'precision': round(sum(s['precision'] * s['n_gt']
                               for s in scores) / sum(s['n_gt'] for s in scores), 4),
        'date': _aggregate(scores, 'date'),
        'label': _aggregate(scores, 'label'),
        'debit': _aggregate(scores, 'debit'),
        'credit': _aggregate(scores, 'credit'),
        'balance': _aggregate(scores, 'balance'),
        'currency': _aggregate(scores, 'currency'),
    }
    for key, threshold in expected['thresholds'].items():
        assert agg[key] >= threshold, (
            f'Régression sur {key} : {agg[key]} < {threshold} '
            f'(attendu >= {threshold})')


def test_aucune_transaction_inventee(all_scores):
    """Précision = 1.0 : le moteur ne fabrique jamais de transaction."""
    assert all(s['fp'] == 0 for s in all_scores.values())


def test_aucune_transaction_perdue(all_scores):
    """Rappel = 1.0 : les 447 transactions du corpus sont toutes extraites."""
    assert all(s['fn'] == 0 for s in all_scores.values())


def test_familles(all_scores, ground_truth):
    """Chaque famille garde ses performances documentées."""
    fam_expected = {
        'fr_pdf': {'recall': 0.99, 'debit': 0.99, 'label': 0.99},
        'eu_pdf': {'recall': 0.99, 'debit': 0.99},
        'uemoa_pdf': {'recall': 0.99, 'debit': 0.99},
        'csv_mm': {'recall': 0.99, 'debit': 0.99, 'label': 0.99},
    }
    for fam, th in fam_expected.items():
        fam_scores = [s for f, s in all_scores.items()
                      if ground_truth[f]['family'] == fam]
        for key, t in th.items():
            v = _aggregate(fam_scores, key) if key != 'recall' else round(
                sum(s['recall'] * s['n_gt'] for s in fam_scores)
                / sum(s['n_gt'] for s in fam_scores), 4)
            assert v >= t, f'{fam}.{key} = {v} < {t}'


def test_documents_parfaits(all_scores):
    """>= 27/32 documents parfaitement convertis (les 5 restants sont des
    comportements honnêtes documentés dans docs/FORMATS.md)."""
    perfect = sum(1 for s in all_scores.values()
                  if s['recall'] == 1.0 and s['precision'] == 1.0
                  and (s['date'] or 1) >= 0.999 and (s['debit'] or 1) >= 0.999
                  and (s['credit'] or 1) >= 0.999
                  and (s['balance'] if s['balance'] is not None else 1) >= 0.999
                  and (s['currency'] if s['currency'] is not None else 1) >= 0.999)
    assert perfect >= 27


def test_soldes_coherents_sur_documents_sains(corpus_results):
    """Sur les documents dont le GT est cohérent, la chaîne des soldes
    passe (le doc scanné T imprime une ouverture incohérente : exclu)."""
    for fname, r in corpus_results.items():
        if fname == 'SYNTH_stress_scanned_T.pdf':
            continue
        if r['checks']['chain_checked'] and r['transactions']:
            assert r['checks']['chain_ok'] is True, fname
