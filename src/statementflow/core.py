"""Primitives partagées : montants, dates, devises, modèles de sortie.

Toutes les fonctions de ce module sont pures et testées unitairement.
Elles proviennent du moteur v2 validé sur le corpus de 32 documents
synthétiques (voir tests/data/corpus) et n'ont pas été modifiées depuis.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

SCHEMA_VERSION = "statementflow/v1"

# bornes d'années acceptées : un relevé bancaire hors de cette plage est
# considéré comme non interprétable (pas une vraie date de relevé)
VALID_YEAR_MIN, VALID_YEAR_MAX = 1900, 2100

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


def _valid_iso_date(y: int, mo: int, d: int) -> Optional[str]:
    """Validation calendaire STRICTE : jour réel du mois (31 avril rejeté),
    années bissextiles (29/02/2023 rejeté, 29/02/2024 accepté), plage
    d'années plausible. Retourne l'ISO ou None — jamais une date fabriquée.
    """
    if not (VALID_YEAR_MIN <= y <= VALID_YEAR_MAX):
        return None
    try:
        _dt.date(y, mo, d)
    except ValueError:  # 31/02, 30/02, 31/04, 31/06, 29/02 non bissextile…
        return None
    return f'{y:04d}-{mo:02d}-{d:02d}'


def parse_date(s: object) -> Optional[str]:
    """Normalise une date en ISO 8601 (YYYY-MM-DD), avec validation
    calendaire stricte : une date impossible (31/02/2026, 29/02/2023,
    31/04/2026…) retourne None — elle sera signalée par une alerte
    « date_unparseable » par le moteur, jamais convertie en une autre date.

    Accepte : ISO « 2026-01-31 » (préfixe), FR/DE/NL « 31/01/2026 »,
    « 31.01.2026 », « 31-01-2026 », années à 2 chiffres (« 31/01/26 »).
    Convention documentée : les formats non-ISO sont interprétés
    JOUR/MOIS (Europe/UEMOA) — voir docs/FORMATS.md.
    """
    if s is None:
        return None
    s = str(s).strip()
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        return _valid_iso_date(int(m.group(1)), int(m.group(2)),
                               int(m.group(3)))
    m = re.match(r'^(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})$', s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        return _valid_iso_date(y, mo, d)
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
    """Transaction normalisée.

    Champs ajoutés en v0.2.0 (rétrocompatibles) :
    - transaction_id : identifiant stable dérivé du contenu du document
      (SHA-256 tronqué) — aucune donnée personnelle ;
    - description : libellé normalisé ; original_description : libellé brut
      source (espaces d'origine conservés) ;
    - amount : montant signé (crédit − débit) quand un montant existe ;
    - alerts : codes d'alerte affectant cette transaction (rempli par le
      moteur après calcul des contrôles) ;
    - transaction_id est posé par le moteur (besoin du document_id).
    """
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
    transaction_id: str = ''
    original_label: str = ''
    alerts: list = field(default_factory=list)

    @property
    def amount(self) -> Optional[float]:
        """Montant signé : crédit − débit. None si aucun montant connu."""
        if self.debit is None and self.credit is None:
            return None
        return (self.credit or 0.0) - (self.debit or 0.0)

    def to_dict(self) -> dict:
        return {
            'index': self.index,
            'transaction_id': self.transaction_id,
            'date': self.date,
            'description': ' '.join(self.label.split()),
            'original_description': ' '.join(self.original_label.split())
                                    if self.original_label else self.label,
            'label': self.label,  # compat v0.1 (== description)
            'debit': self.debit,
            'credit': self.credit,
            'amount': round(self.amount, 2) if self.amount is not None else None,
            'balance': self.balance,
            'currency': self.currency,
            'confidence': round(self.confidence, 3),
            'alerts': list(self.alerts),
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


def document_id_for(data: bytes) -> str:
    """Identifiant stable du document : SHA-256 tronqué du contenu.
    Aucune donnée personnelle : seul le hash du fichier est exposé."""
    return hashlib.sha256(data).hexdigest()[:16]


def transaction_id_for(document_id: str, index: int, date: Optional[str],
                       debit: Optional[float],
                       credit: Optional[float]) -> str:
    """Identifiant stable d'une transaction : hash du document + position +
    données clés. Rejouer le même document donne les mêmes identifiants."""
    seed = f'{document_id}|{index}|{date}|{debit}|{credit}'
    return hashlib.sha256(seed.encode()).hexdigest()[:16]
