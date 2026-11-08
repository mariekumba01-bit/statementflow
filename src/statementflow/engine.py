"""Moteur StatementFlow : PDF / CSV / XLSX → transactions JSON normalisées.

Pipeline : détection du format → adaptateur → extraction (texte ou OCR) →
normalisation → contrôles de cohérence → alertes/confiance → résultat JSON.

Correctifs apportés par rapport au moteur v2 validé (voir tests/test_fixes.py) :
1. entêtes de continuation « (suite) » des pages suivantes ignorées
   (elles polluaient les libellés) ;
2. lignes avec montants mais date non parsable extraites avec date=null et
   signalées (au lieu d'être perdues) ;
3. OCR : fusion des fragments de milliers à 8 pt + clustering top-k par
   cardinalité + appariement ordonné clusters↔colonnes (débit/crédit) ;
4. devise jamais inventée : null + alerte « currency_not_identified »,
   avec possibility de fournir un indice explicite (currency_hint).

Les métriques du corpus de test (32 documents synthétiques, 447 transactions)
sont mesurées par tests/test_engine.py et figées dans tests/expected_metrics.json.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from .adapters import Adapter, detect_adapter
from .checks import (compute_checks, deduplicate, document_confidence,
                     validation_status)
from .config import Config
from .core import (Alert, NUMERIC_ROLES, ROLE_KEYS, SCHEMA_VERSION,
                   Transaction, detect_currency, document_id_for,
                   normalize_txt, parse_amount, parse_date,
                   transaction_id_for)
from .pdf_backend import OCRUnavailable, extract_lines
from .tabular import map_headers, read_tabular

PARSER_NAME = 'generic-columns-v3'

# alias de devises écrits différemment selon les exports (cellule « FCFA »,
# symbole isolé…) → code ISO pour permettre la comparaison de cohérence
CURRENCY_ALIASES = {'FCFA': 'XOF', 'CFA': 'XOF',
                    '€': 'EUR', '$': 'USD', '£': 'GBP'}


def _norm_currency(c: Optional[str]) -> Optional[str]:
    """Normalise une devise de cellule vers le code ISO utilisé en interne."""
    if not c:
        return None
    c = str(c).strip().upper()
    return CURRENCY_ALIASES.get(c, c)

PDF_MAGIC = b'%PDF-'
XLSX_MAGIC = b'PK\x03\x04'

PAGE_CONT_RE = re.compile(r'\((suite|continued|fortsetzung|cont\.?)\)', re.I)
DATE_LIKE_RE = re.compile(r'^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\.?$')
TOTAL_RE = re.compile(r'\btotal\b', re.I)
SOLDE_WORD_RE = re.compile(r'solde|saldo|balance', re.I)
SUMMARY_SOLD_RE = re.compile(
    r'solde\s+au|solde\s*:|saldo\s+am|balance\s+as|solde\s+(final|de cl)', re.I)
AMOUNT_HEADER_RE = re.compile(
    r'débit|debit|crédit|credit|retrait|dépôt|montant|importe|amount|betrag', re.I)
OPENING_RE = re.compile(
    r'solde|saldo|balance|ancien|ouverture|initial|ant(é|e)rieur', re.I)

# sens d'une transaction quand aucune colonne débit/crédit n'existe :
# 1) colonne « Sens/Type » explicite ; 2) mots-clés du libellé ; 3) débit + alerte.
CREDIT_WORDS = re.compile(
    r'rec(u|oi|eption)|cash-in|received|credit|deposit|encaiss|remboursement|p2p', re.I)
DIRECTION_CREDIT = {'reception', 'recu', 'received', 'credit', 'in', 'depot',
                    'deposit', 'entree', 'incoming'}
DIRECTION_DEBIT = {'envoi', 'sent', 'debit', 'out', 'retrait', 'withdrawal',
                   'outgoing'}


class ParseError(ValueError):
    """Erreur de parsing compréhensible (fichier invalide, limites dépassées…)."""


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def _classify_header(line) -> Optional[dict]:
    """Entête de tableau ? → {role: x_centre} (comparaison sans accents)."""
    toks = [(normalize_txt(t[1]), (t[2] + t[3]) / 2) for t in line]
    roles = {}
    for i, (t, x) in enumerate(toks):
        for role, keys in ROLE_KEYS.items():
            if role in roles:
                continue
            for k in keys:
                if t == k or (len(k) > 3 and k in t):
                    roles[role] = x
                    break
        if i + 1 < len(toks):
            big = t + ' ' + toks[i + 1][0]
            for role, keys in ROLE_KEYS.items():
                if role in roles:
                    continue
                for k in keys:
                    if ' ' in k and big == k:
                        roles[role] = (x + toks[i + 1][1]) / 2
                        break
    return roles if len(roles) >= 2 else None


def _cluster_x1(rows_tokens, tol=12.0):
    """Clusterise les bords droits des montants → [(centre, cardinalité)]."""
    xs = sorted(t['x1'] for toks in rows_tokens for t in toks)
    clusters = []
    for x in xs:
        if clusters and x - clusters[-1][-1] < tol:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    return [(sum(c) / len(c), len(c)) for c in clusters]


def _assign_roles(clusters, header):
    """Apparie clusters et rôles numériques.

    - autant de clusters que de rôles : appariement ordonné (validé sur le
      corpus v2) ;
    - plus de clusters (bruit OCR, fragments) : les k clusters les plus
      peuplés, puis appariement ordonné — les vraies colonnes sont les plus
      fréquentes ;
    - moins : appariement partiel par la gauche.
    """
    numeric_roles = sorted((r for r in NUMERIC_ROLES if r in header),
                           key=lambda r: header[r])
    if not numeric_roles:
        return {}
    if len(clusters) >= len(numeric_roles):
        if len(clusters) > len(numeric_roles):
            clusters = sorted(
                sorted(clusters, key=lambda c: -c[1])[:len(numeric_roles)],
                key=lambda c: c[0])
        return {role: c for role, (c, _n) in zip(numeric_roles, clusters)}
    return {role: c for role, (c, _n) in zip(numeric_roles, clusters)}


def _line_numbers(line) -> list:
    vals = []
    for t in line:
        v = parse_amount(t[1])
        if v is not None and parse_date(t[1]) is None:
            vals.append(v)
    return vals


def _parse_pdf(data: bytes, filename: str, config: Config,
               currency_hint: Optional[str]):
    try:
        lines, page_infos, n_pages = extract_lines(data, ocr_dpi=config.ocr_dpi)
    except OCRUnavailable as e:  # scanné sans OCR disponible
        raise ParseError(
            f'PDF sans couche texte et OCR indisponible ({e}). '
            f'Installez tesseract et poppler-utils pour traiter les PDF scannés.') from e
    except Exception as e:  # pdfminer : PDF corrompu, chiffré, invalide…
        raise ParseError(f'PDF illisible ou corrompu : {e}') from e
    if n_pages > config.max_pages:
        raise ParseError(
            f'Document de {n_pages} pages (limite : {config.max_pages}).')

    header, header_idx = None, -1
    for i, line in enumerate(lines):
        roles = _classify_header(line)
        if roles and any(r in roles for r in ('date', 'amount', 'debit')):
            header, header_idx = roles, i
            break

    full_text = ' '.join(t[1] for l in lines for t in l)
    currency = detect_currency(full_text) or currency_hint

    opening = None
    for line in lines[:max(header_idx, 0)]:
        j = ' '.join(t[1] for t in line)
        if SOLDE_WORD_RE.search(j) and _line_numbers(line):
            opening = _line_numbers(line)[-1]

    alerts: list[Alert] = []
    if any(p['method'] == 'ocr' for p in page_infos):
        ocr_pages = [i + 1 for i, p in enumerate(page_infos) if p['method'] == 'ocr']
        alerts.append(Alert(
            'ocr_used', 'info',
            f'OCR appliqué aux pages sans couche texte : {ocr_pages}.'))
        for i, p in enumerate(page_infos):
            if p['method'] == 'ocr' and (p['ocr_confidence'] or 0) < 80:
                alerts.append(Alert(
                    'ocr_low_confidence', 'warning',
                    f'Confiance OCR faible sur la page {i + 1} '
                    f'({p["ocr_confidence"]}/100) : vérifiez les montants.'))

    first_num_x = min((header[r] for r in NUMERIC_ROLES if header and r in header),
                      default=1e9)

    # ── pré-analyse des lignes de données ──────────────────────────────
    raw_rows = []  # dicts provisoires
    closing, closing_derived = None, True
    prev_row = None
    for li in range(header_idx + 1, len(lines)):
        line = lines[li]
        text_join = ' '.join(t[1] for t in line)
        if not text_join.strip() or _classify_header(line):
            continue
        if PAGE_CONT_RE.search(text_join):
            alerts.append(Alert(
                'page_header_skipped', 'info',
                f'Entête de continuation ignorée (ligne {li + 1}).'))
            continue
        date_tok, date_like_tok = None, None
        for t in line:
            if parse_date(t[1]):
                date_tok = t
                break
        if date_tok is None and line and DATE_LIKE_RE.fullmatch(line[0][1].strip()):
            date_like_tok = line[0]
        toks, label_parts = [], []
        for t in line:
            if t is date_tok:
                continue
            if re.fullmatch(r'(?:EUR|XOF|FCFA|GBP|USD|CFA|CHF|€|\$|£)',
                            t[1].strip(), re.I):
                continue  # suffixe de devise isolé
            v = parse_amount(t[1])
            if v is None or t[3] < first_num_x - 10 or t is date_like_tok:
                if t is not date_like_tok:
                    label_parts.append(t[1])
            else:
                toks.append({'x1': t[3], 'v': v})
        nums = [t['v'] for t in toks]

        if date_tok is None:
            if TOTAL_RE.search(text_join):
                continue
            if SUMMARY_SOLD_RE.search(text_join) \
                    and not AMOUNT_HEADER_RE.search(text_join) and nums:
                closing, closing_derived = nums[-1], False
                continue
            if SOLDE_WORD_RE.search(text_join) and nums:
                closing, closing_derived = nums[-1], False
                continue
            if nums and (date_like_tok is not None or label_parts):
                # correctif n°2 : ligne avec montants mais date non parsable
                raw_rows.append({
                    'date': None, 'date_raw': date_like_tok[1] if date_like_tok else None,
                    'label': ' '.join(label_parts), 'toks': toks,
                    'page': line[0][0] + 1 if line else 1, 'source_row': li + 1,
                })
                continue
            if prev_row is not None and not nums and len(text_join) < 60 \
                    and not SOLDE_WORD_RE.search(text_join):
                prev_row['label'] += ' ' + text_join.strip()
            continue
        if SUMMARY_SOLD_RE.search(text_join) \
                and not AMOUNT_HEADER_RE.search(text_join):
            if nums:
                closing, closing_derived = nums[-1], False
            continue
        raw_rows.append({
            'date': parse_date(date_tok[1]), 'date_raw': None,
            'label': ' '.join(label_parts), 'toks': toks,
            'page': date_tok[0] + 1, 'source_row': li + 1,
        })
        prev_row = raw_rows[-1]

    # ── appariement clusters ↔ colonnes ────────────────────────────────
    role_centers = _assign_roles(_cluster_x1([r['toks'] for r in raw_rows]),
                                 header or {})

    def role_of_x1(x1):
        best, bd = None, 1e9
        for r, c in role_centers.items():
            if abs(x1 - c) < bd:
                best, bd = r, abs(x1 - c)
        return best

    transactions: list[Transaction] = []
    for r in raw_rows:
        amounts = {k: None for k in NUMERIC_ROLES}
        for t in r['toks']:
            role = role_of_x1(t['x1'])
            if role and amounts[role] is None:
                amounts[role] = t['v']
        debit, credit = amounts['debit'], amounts['credit']
        if amounts['amount'] is not None:
            v = amounts['amount']
            if v < 0:
                debit, credit = -v, None
            else:
                credit, debit = v, None
        page_info = page_infos[r['page'] - 1] if r['page'] <= len(page_infos) else {}
        method = page_info.get('method', 'text')
        conf = 1.0 if method == 'text' else 0.75
        if method == 'ocr' and (page_info.get('ocr_confidence') or 0) < 80:
            conf = 0.6
        if r['date'] is None:
            conf -= 0.25
        if debit is None and credit is None:
            conf -= 0.25
        tx = Transaction(
            index=len(transactions) + 1, date=r['date'], label=r['label'],
            debit=debit, credit=credit, balance=amounts['balance'],
            currency=None, confidence=max(0.1, min(1.0, conf)),
            page=r['page'], method=method, source_row=r['source_row'])
        transactions.append(tx)
        if amounts['balance'] is not None:
            closing, closing_derived = amounts['balance'], True
        if debit is not None and credit is not None:
            alerts.append(Alert(
                'debit_and_credit_ambiguous', 'warning',
                f'Débit ET crédit reconnus sur la même ligne (colonnes '
                f'adjacentes confondues ?) : les deux valeurs sont conservées, '
                f'verifiez cette transaction.',
                transaction_index=tx.index))
        if r['date'] is None and r.get('date_raw'):
            alerts.append(Alert(
                'date_unparseable', 'error',
                f'Date non interprétable « {r["date_raw"]} » (hors bornes de '
                f'calendrier ?) : transaction conservée avec date=null.',
                transaction_index=tx.index))
        elif r['date'] is None:
            alerts.append(Alert(
                'undated_amount_line', 'warning',
                'Ligne avec montants mais sans date : conservée avec date=null.',
                transaction_index=tx.index))
        if debit is None and credit is None:
            alerts.append(Alert(
                'amount_missing', 'warning',
                'Aucun montant reconnu sur cette ligne datée.',
                transaction_index=tx.index))

    method_summary = ('text' if all(p['method'] == 'text' for p in page_infos)
                      else 'ocr' if all(p['method'] == 'ocr' for p in page_infos)
                      else 'mixed')
    return transactions, currency, opening, closing, closing_derived, alerts, \
        n_pages, method_summary


# ---------------------------------------------------------------------------
# CSV / XLSX
# ---------------------------------------------------------------------------
def _direction_of(text: str) -> Optional[str]:
    """Sens d'après une colonne « Sens/Type » ou un libellé : 'credit'|'debit'."""
    t = normalize_txt(text)
    if not t:
        return None
    if 'cash-in' in t or 'cash in' in t:
        return 'credit'
    if 'cash-out' in t or 'cash out' in t:
        return 'debit'
    words = set(re.split(r'[^a-z0-9]+', t))
    if words & DIRECTION_CREDIT:
        return 'credit'
    if words & DIRECTION_DEBIT:
        return 'debit'
    if CREDIT_WORDS.search(t):
        return 'credit'
    return None


def _parse_tabular(data: bytes, filename: str, config: Config,
                   currency_hint: Optional[str]):
    try:
        rows, _is_xlsx = read_tabular(data, filename, max_rows=config.max_rows)
    except ValueError as e:
        raise ParseError(str(e)) from e
    if len(rows) > config.max_rows:
        raise ParseError(
            f'{len(rows)} lignes (limite : {config.max_rows}).')

    header, hidx = None, None
    for i, r in enumerate(rows[:10]):
        m = map_headers(r)
        if m:
            header, hidx = m, i
            break

    full_text = ' | '.join(str(c) for r in rows for c in r)
    currency = detect_currency(full_text) or currency_hint
    has_currency_col = bool(header and 'currency' in header.values())

    transactions: list[Transaction] = []
    alerts: list[Alert] = []
    recs = []
    saw_negative_amount = False  # colonne « montant » signée ? (parenthèses / -)
    for ri, r in enumerate(rows[hidx + 1:] if header else rows, start=(hidx or 0) + 2):
        if not any(str(c).strip() for c in r):
            continue
        joined = ' | '.join(str(c) for c in r)
        if TOTAL_RE.search(joined):
            continue
        rec = {'date': None, 'date_raw': None, 'label': '', 'debit': None,
               'credit': None, 'balance': None, 'currency': None,
               'direction': None, 'source_row': ri}
        label_parts = []
        direction_parts = []  # texte de la colonne Sens/Type : libellé de repli
        for i, cell in enumerate(r):
            cell = str(cell).strip()
            role = header.get(i) if header else None
            if role in ('skip',):
                continue
            if role == 'date' and rec['date'] is None:
                rec['date'] = parse_date(cell)
                if rec['date'] is None and cell and DATE_LIKE_RE.fullmatch(cell):
                    rec['date_raw'] = cell
                continue
            if role == 'currency':
                rec['currency'] = _norm_currency(cell)
                continue
            if role == 'direction':
                d = _direction_of(cell)
                if d:
                    rec['direction'] = d
                if cell:
                    direction_parts.append(cell)
                continue
            v = parse_amount(cell) if cell else None
            if role in ('debit', 'credit', 'amount') and v == 0:
                v = None  # « 0 » = colonne vide dans de nombreux exports
            if role in ('debit', 'credit', 'balance'):
                rec[role] = v
            elif role == 'amount':
                if v is not None and v < 0:
                    saw_negative_amount = True
                    rec['debit'], rec['credit'] = -v, None
                else:
                    rec['credit'], rec['debit'] = v, None
            elif role in ('label', None):
                if cell and parse_date(cell) is None:
                    label_parts.append(cell)
        rec['label'] = ' '.join(label_parts) or ' '.join(direction_parts)
        if rec['date'] is None and rec['date_raw'] is None:
            continue
        recs.append(rec)

    # colonne « montant » non signée (aucun négatif dans tout le document) :
    # le sens ne peut pas venir du signe → colonne Sens, puis mots-clés du
    # libellé, sinon débit supposé AVEC alerte. Une colonne signée garde la
    # convention signe<0 = débit, signe>0 = crédit.
    has_credit_col = bool(header and 'credit' in header.values())
    for rec in recs:
        rec['side_assumed'] = False
        if rec['credit'] is not None and rec['debit'] is None \
                and not has_credit_col and not saw_negative_amount:
            side = rec['direction'] or _direction_of(rec['label'])
            if side == 'debit' or side is None:
                rec['side_assumed'] = side is None
                rec['debit'], rec['credit'] = rec['credit'], None

    for rec in recs:
        if rec['side_assumed']:
            # l'alerte est créée quand la transaction existe : index correct
            alerts.append(Alert(
                'side_assumed_debit', 'warning',
                'Montant positif sans signal de direction : sens '
                'supposé « débit » — vérifiez cette transaction.',
                transaction_index=len(transactions) + 1))
        if rec['debit'] is None and rec['credit'] is None:
            if rec['balance'] is None and not rec['label']:
                continue
            alerts.append(Alert(
                'amount_missing', 'warning',
                'Aucun montant reconnu sur cette ligne datée.',
                transaction_index=len(transactions) + 1))
        if rec['debit'] is not None and rec['credit'] is not None:
            # une cellule ne peut pas être débit ET crédit : garder le débit
            rec['credit'] = None
        if rec['date'] is None and rec['date_raw']:
            alerts.append(Alert(
                'date_unparseable', 'error',
                f'Date non interprétable « {rec["date_raw"]} » : '
                f'transaction conservée avec date=null.',
                transaction_index=len(transactions) + 1))
        conf = 1.0
        if rec['date'] is None:
            conf -= 0.25
        if rec['debit'] is None and rec['credit'] is None:
            conf -= 0.25
        tx = Transaction(
            index=len(transactions) + 1, date=rec['date'], label=rec['label'],
            debit=rec['debit'], credit=rec['credit'], balance=rec['balance'],
            currency=rec['currency'], confidence=max(0.1, min(1.0, conf)),
            page=1, method='csv-row', source_row=rec['source_row'])
        transactions.append(tx)

    closing = None
    closing_derived = False
    for tx in reversed(transactions):
        if tx.balance is not None:
            closing, closing_derived = tx.balance, True
            break
    return transactions, currency, None, closing, closing_derived, alerts, 1, 'tabular'


# ---------------------------------------------------------------------------
# Façade
# ---------------------------------------------------------------------------
def detect_kind(filename: str, data: bytes) -> str:
    head = data[:1024]
    if PDF_MAGIC in head:
        return 'pdf'
    if filename.lower().endswith('.pdf'):
        raise ParseError(
            'Extension .pdf mais le fichier n\'est pas un PDF '
            '(signature %PDF absente).')
    if head.startswith(XLSX_MAGIC) or filename.lower().endswith(('.xlsx', '.xls')):
        return 'xlsx'
    if filename.lower().endswith(('.csv', '.tsv', '.txt')):
        return 'csv'
    # pas d'extension connue : tenter un décodage texte
    try:
        data.decode('utf-8')
        return 'csv'
    except UnicodeDecodeError:
        raise ParseError(
            'Format non reconnu (ni PDF, ni XLSX, ni CSV/TSV/texte lisible).')


def parse_bytes(data: bytes, filename: str, config: Config | None = None,
                currency_hint: Optional[str] = None) -> dict:
    """Point d'entrée principal : octets + nom de fichier → résultat complet."""
    config = config or Config()
    if len(data) > config.max_file_mb * 1024 * 1024:
        raise ParseError(
            f'Fichier de {len(data) / 1024 / 1024:.1f} Mo '
            f'(limite : {config.max_file_mb} Mo).')
    if not data:
        raise ParseError('Fichier vide.')

    kind = detect_kind(filename, data)
    _last_rows_count = 0
    if kind == 'pdf':
        transactions, currency, opening, closing, closing_derived, alerts, \
            pages, method = _parse_pdf(data, filename, config, currency_hint)
        adapter = detect_adapter('pdf', '', [])
    else:
        transactions, currency, opening, closing, closing_derived, alerts, \
            pages, method = _parse_tabular(data, filename, config, currency_hint)
        try:
            all_rows = read_tabular(data, filename)[0]
            _last_rows_count = len(all_rows)
            sample_rows = all_rows[:5]
        except (ValueError, ParseError):
            sample_rows = []
        adapter = detect_adapter('tabular', '', sample_rows)

    # normalisation du libellé (espaces multiples) — l'original est conservé
    # dans original_description (traçabilité de la source)
    for tx in transactions:
        tx.original_label = tx.label
        tx.label = ' '.join(tx.label.split())

    # correctif n°4 : devise jamais inventée
    if currency is None and transactions:
        alerts.append(Alert(
            'currency_not_identified', 'warning',
            'Aucune devise identifiable dans le document (currency=null). '
            'Fournissez currency_hint si vous connaissez la devise du compte.'))
    if opening is None and transactions:
        alerts.append(Alert(
            'opening_missing', 'info',
            'Solde d\'ouverture non trouvé dans le document.'))
    if closing is None and transactions:
        alerts.append(Alert(
            'closing_missing', 'info',
            'Solde de clôture non trouvé dans le document.'))
    for tx in transactions:
        tx.currency = _norm_currency(tx.currency) or currency
    # cohérence de devise (v0.2.0) : cellule de devise contredisant la devise
    # du document → avertissement (ex. ligne « EUR » dans un compte XOF)
    for tx in transactions:
        if tx.currency and currency and tx.currency != currency:
            alerts.append(Alert(
                'currency_mismatch', 'warning',
                f'Devise « {tx.currency} » sur la transaction {tx.index} '
                f'contredit la devise du document ({currency}).',
                transaction_index=tx.index))

    # doublons puis contrôles comptables : les alertes de parsing émises
    # avant la déduplication sont remappées sur les index finaux
    pre_dedupe_alerts = alerts
    transactions, dedupe_alerts, index_map = deduplicate(transactions)
    for a in pre_dedupe_alerts:
        if a.transaction_index is not None:
            a.transaction_index = index_map.get(a.transaction_index)
    checks, check_alerts = compute_checks(
        transactions, opening, closing, config.tolerance)
    alerts = pre_dedupe_alerts
    alerts.extend(dedupe_alerts)
    alerts.extend(check_alerts)

    # identifiants stables + alertes par transaction (v0.2.0) — indexation
    # des alertes en O(n) (un rescann par transaction serait quadratique)
    doc_id = document_id_for(data)
    alerts_by_index: dict[int, set] = {}
    for a in alerts:
        if a.transaction_index is not None:
            alerts_by_index.setdefault(a.transaction_index, set()).add(a.code)
    for tx in transactions:
        tx.transaction_id = transaction_id_for(
            doc_id, tx.index, tx.date, tx.debit, tx.credit)
        tx.alerts = sorted(alerts_by_index.get(tx.index, ()))
    dates = sorted(t.date for t in transactions if t.date)

    return {
        'schema': SCHEMA_VERSION,
        'document': {
            'document_id': doc_id,
            'schema_version': SCHEMA_VERSION,
            'filename': os.path.basename(filename),
            'kind': kind,
            'pages': pages,
            'method': method,
            'parser': PARSER_NAME,
            'adapter': adapter.name,
            'statement_period': {'start': dates[0], 'end': dates[-1]}
                                if dates else None,
            'account_currency': currency,
        },
        'metadata': {
            'date_interpretation': 'JOUR/MOIS (Europe/UEMOA) pour les formats '
                                   'non ISO ; ISO YYYY-MM-DD accepté tel quel',
            'confidence_rule': 'heuristique documentée (texte 1.0, OCR 0.75, '
                               'OCR faible 0.6 ; −0.25 par donnée manquante ; '
                               'plafond 0.5 si alerte d\'erreur) — non calibrée',
        },
        'currency': currency,
        'opening_balance': opening,
        'closing_balance': closing,
        'closing_derived': closing_derived,
        'transactions': [tx.to_dict() for tx in transactions],
        'totals': checks['totals'],
        'checks': checks,
        'validation': validation_status(checks, alerts),
        'alerts': [a.to_dict() for a in alerts],
        'confidence': document_confidence(transactions, alerts),
        'usage': {'pages': pages, 'transactions': len(transactions),
                  # lignes du document tabulaire (0 pour un PDF) — la
                  # facturation publique est « 1 crédit / 50 lignes »,
                  # indépendante du nombre de transactions reconnues
                  'rows': _last_rows_count},
    }


def parse_file(path: str, config: Config | None = None,
               currency_hint: Optional[str] = None) -> dict:
    """Parse un fichier local (lecture seule, aucune copie conservée)."""
    with open(path, 'rb') as f:
        data = f.read()
    return parse_bytes(data, os.path.basename(path), config, currency_hint)
