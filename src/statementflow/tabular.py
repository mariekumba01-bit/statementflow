"""Lecture tabulaire : CSV / TSV / XLSX.

- Détection du séparateur (tabulation, point-virgule, barre verticale,
  virgule) par score d'entêtes reconnues + cohérence du nombre de colonnes ;
- BOM UTF-8 géré ; entête recherchée dans les 10 premières lignes ;
- XLSX lu via openpyxl (aucune dépendance pandas).
"""
from __future__ import annotations

import csv
import io
import re
import zipfile

from .core import ROLE_KEYS, normalize_txt

# Synonymes d'entêtes (comparés via normalize_txt : sans accents, minuscules)
SYNONYMS = {
    'date': {'date', 'datum', 'fecha', 'timestamp'},
    'label': {'libelle', 'label', 'description', 'omschrijving', 'concepto',
              'beschreibung', 'operation'},
    'debit': {'debit', 'retrait', 'withdrawal', 'af', 'soll', 'money out'},
    'credit': {'credit', 'depot', 'deposit', 'bij', 'haben', 'money in'},
    'balance': {'solde', 'saldo', 'balance', 'new balance', 'new balance (xof)'},
    'amount': {'montant', 'importe', 'amount', 'betrag', 'montant (fcfa)',
               'montant (xof)', 'amount (xof)'},
    'currency': {'devise', 'currency'},
    # « Sens » / « Type » : sens de l'opération quand la valeur est explicite
    # (RECEPTION/ENVOI, Debit/Credit) — sert aussi de libellé de repli
    'direction': {'sens', 'type', 'direction'},
    'skip': {'frais', 'fee', 'transaction id', 'heure'},
}

STRONG_LABELS = {'libelle', 'label', 'description', 'omschrijving', 'concepto',
                 'beschreibung'}

XLSX_MAGIC = b'PK\x03\x04'


def map_headers(row) -> dict | None:
    """Mappe les index de colonnes vers des rôles.

    Retourne {index: role} si une entête plausible est trouvée, sinon None.
    « Type »/« Operation » cèdent la place à un vrai libellé s'il existe.
    """
    mapping = {}
    for i, cell in enumerate(row):
        c = normalize_txt(cell).replace('\ufeff', '')
        # « withdrawal (xof) » → « withdrawal »
        c_main = re.sub(r'\s*\([^)]*\)\s*$', '', c).strip()
        if c_main != c and c_main in {k for keys in SYNONYMS.values() for k in keys}:
            c = c_main
        for role, keys in SYNONYMS.items():
            if c in keys:
                mapping[i] = role
                break
    label_cols = [i for i, r in mapping.items() if r == 'label']
    strong = [i for i in label_cols
              if normalize_txt(row[i]) in STRONG_LABELS]
    if len(label_cols) > 1 and strong:
        for i in label_cols:
            if i not in strong:
                mapping[i] = 'skip'
    roles = set(mapping.values())
    if 'date' in roles and (len(roles) >= 3 or {'amount', 'debit', 'credit'} & roles):
        return mapping
    return None


def read_tabular(data: bytes, filename: str) -> tuple[list, bool]:
    """Lit un CSV/TSV/XLSX → (lignes, est_xlsx).

    Chaque ligne est une liste de chaînes. Lève ValueError si illisible.
    """
    if filename.lower().endswith(('.xlsx', '.xls')) or data.startswith(XLSX_MAGIC):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True,
                                        data_only=True)
            ws = wb.active
            rows = [[('' if c is None else str(c)) for c in r]
                    for r in ws.iter_rows(values_only=True)]
            wb.close()
            return rows, True
        except zipfile.BadZipFile as e:
            raise ValueError(f'Fichier XLSX invalide : {e}') from e
    # CSV / TSV : essayer utf-8-sig puis latin-1
    text = None
    for enc in ('utf-8-sig', 'utf-8', 'latin-1'):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError('Encodage non supporté (ni UTF-8 ni Latin-1)')
    best, best_key = None, (-1, 10 ** 9)
    for sep in ['\t', ';', '|', ',']:
        rows = list(csv.reader(io.StringIO(text), delimiter=sep))
        score = sum(1 for r in rows[:10] if map_headers(r) is not None)
        widths = [len(r) for r in rows if r]
        consistency = (max(widths) - min(widths)) if widths else 999
        if score > best_key[0] or (score == best_key[0] and consistency < best_key[1]):
            best, best_key = rows, (score, consistency)
    if best is None:
        raise ValueError('Aucun séparateur CSV exploitable')
    return best, False


def header_role_keys():
    """Clés de rôles exposées pour les adaptateurs."""
    return dict(ROLE_KEYS)
