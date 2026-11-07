"""Primitives partagées : montants, dates, devises, modèles de sortie.

Toutes les fonctions de ce module sont pures et testées unitairement.
Elles proviennent du moteur v2 validé sur le corpus de 32 documents
synthétiques (voir tests/data/corpus) et n'ont pas été modifiées depuis.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

SCHEMA_VERSION = "statementflow/v1"

# ---------------------------------------------------------------------------
# Expressions régulières
# ---------------------------------------------------------------------------
CUR_RE = re.compile(r'(?:EUR|XOF|FCFA|GBP|USD|CFA|CHF|€|\$|£)', re.IGNORECASE)
# jeton « numérique » : chiffres, séparateurs, espaces (milliers), parenthèses, signe
NUM_RE = re.compile(r'^[\d.,\u00a0 ()\-]+$')

CURRENCY_DETECT = [
    ('XOF', re.compile(r'XOF|FCFA', re.I)),
    ('EUR', re.compile(r'EUR|€')),
    ('GBP', re.compile(r'GBP|£')),
    ('USD', re.compile(r'USD|\$')),
]


def normalize_txt(s: object) -> str:
    """Minuscule + suppression des accents (comparaison d'entêtes robuste
    aux accents perdus en OCR : « Débit » → « debit »)."""
    s = unicodedata.normalize('NFKD', str(s or ''))
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip()


def parse_amount(s: object) -> Optional[float]:
    """Convertit un montant dans l'un des formats gérés en float signé.

    Formats : FR « 1 234,56 », EU « 1.234,56 », UK « 1,234.56 »,
    XOF entier « 1 234 567 », suffixes de devise (« 1 234,56 EUR »,
    « 1 234 567 XOF »), parenthèses comptables « (1 234,56) » et
    signe suffixe « 1.234,56- ». Retourne None si non parsable.
    """
    if s is None:
        return None
    s = str(s).strip()
    if not s or s in {'-', '.', '--'}:
        return None
    neg = False
    if s.startswith('(') and s.endswith(')'):
        neg, s = True, s[1:-1].strip()
    s = CUR_RE.sub('', s).strip()
    if s.endswith('-'):
        neg, s = True, s[:-1].strip()
    if s.startswith('-'):
        neg, s = True, s[1:].strip()
    if not s or not re.search(r'\d', s):
        return None
    if ',' in s and '.' in s:
        dec = ',' if s.rfind(',') > s.rfind('.') else '.'
    elif ',' in s:
        dec = ',' if len(s.split(',')[-1]) in (1, 2) else None
    elif '.' in s:
        dec = '.' if len(s.split('.')[-1]) in (1, 2) else None
    else:
        dec = None
    if dec == ',':
        s = s.replace('.', '').replace(',', '.')
    elif dec == '.':
        s = s.replace(',', '')
    else:
        s = s.replace(',', '').replace('.', '')
    s = s.replace(' ', '').replace('\u00a0', '')
    m = re.search(r'\d+(?:\.\d{1,2})?', s)
    if not m:
        return None
    v = float(m.group(0))
    return -v if neg else v


def parse_date(s: object) -> Optional[str]:
    """Normalise une date en ISO 8601 (YYYY-MM-DD).

    Accepte : ISO « 2026-01-31 » (préfixe), FR/DE/NL « 31/01/2026 »,
    « 31.01.2026 », « 31-01-2026 », années à 2 chiffres (« 31/01/26 »).
    """
    if s is None:
        return None
    s = str(s).strip()
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f'{y:04d}-{mo:02d}-{d:02d}'
        return None  # ISO mais hors bornes de calendrier
    m = re.match(r'^(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})$', s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f'{y:04d}-{mo:02d}-{d:02d}'
    return None


def detect_currency(text: str) -> Optional[str]:
    """Devise la plus fréquente dans un texte, ou None si aucune :
    la devise n'est JAMAIS inventée."""
    best, n = None, 0
    for code, rx in CURRENCY_DETECT:
        c = len(rx.findall(text or ''))
        if c > n:
            best, n = code, c
    return best


# ---------------------------------------------------------------------------
# Rôles de colonnes (entêtes multilingues, comparées SANS accents)
# ---------------------------------------------------------------------------
ROLE_KEYS = {
    'date': ['date', 'datum', 'fecha'],
    'label': ['libelle', 'operation', 'description', 'omschrijving',
              'buchungstext', 'concepto', 'label'],
    'debit': ['debit', 'retrait', 'soll', 'money out', 'withdrawal', 'af'],
    'credit': ['credit', 'depot', 'deposit', 'haben', 'money in', 'bij'],
    'balance': ['solde', 'saldo', 'balance'],
    'amount': ['montant', 'importe', 'amount', 'betrag'],
}

NUMERIC_ROLES = ('debit', 'credit', 'amount', 'balance')


# ---------------------------------------------------------------------------
# Modèles de sortie
# ---------------------------------------------------------------------------
@dataclass
class Alert:
    """Alerte sur une donnée incertaine, ambiguë ou manquante."""
    code: str
    severity: str  # 'info' | 'warning' | 'error'
    message: str
    transaction_index: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            'code': self.code,
            'severity': self.severity,
            'message': self.message,
            'transaction_index': self.transaction_index,
        }


@dataclass
class Transaction:
    """Transaction normalisée."""
    index: int
    date: Optional[str]
    label: str
    debit: Optional[float]
    credit: Optional[float]
    balance: Optional[float]
    currency: Optional[str]
    confidence: float
    page: int = 1
    method: str = 'text'  # 'text' | 'ocr' | 'csv-row'
    source_row: int = 0

    def to_dict(self) -> dict:
        return {
            'index': self.index,
            'date': self.date,
            'label': self.label,
            'debit': self.debit,
            'credit': self.credit,
            'balance': self.balance,
            'currency': self.currency,
            'confidence': round(self.confidence, 3),
            'trace': {
                'page': self.page,
                'method': self.method,
                'source_row': self.source_row,
            },
        }


def label_similarity(a: str, b: str) -> float:
    """Similarité de libellés normalisés (espaces et casse ignorés)."""
    import difflib
    na = ' '.join(normalize_txt(a).split())
    nb = ' '.join(normalize_txt(b).split())
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()
