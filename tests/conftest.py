"""Configuration pytest : accès au package src et au corpus."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

import pytest  # noqa: E402

CORPUS_DIR = os.path.join(ROOT, 'tests', 'data', 'corpus')


@pytest.fixture(scope='session')
def corpus_dir():
    return CORPUS_DIR


@pytest.fixture(scope='session')
def ground_truth():
    import json
    with open(os.path.join(CORPUS_DIR, 'ground_truth.json'), encoding='utf-8') as f:
        return json.load(f)
