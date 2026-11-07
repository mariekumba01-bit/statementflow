"""Extraction de lignes et de tokens depuis un PDF (texte natif ou OCR).

- Couche texte : pdfplumber (aucune dépendance système).
- Couche OCR : pdftoppm (poppler-utils) + tesseract, utilisée uniquement
  quand une page ne contient pas de couche texte exploitable.
- Fusion des fragments numériques scindés par les espaces de milliers
  (« 1 » + « 234,56 » → « 1 234,56 », « (1 » + « 234,56) » → « (1 234,56) »).
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import tempfile
from typing import Optional

from .core import NUM_RE

LINE_TOL = 4.5  # tolérance verticale de regroupement en lignes (points)
MERGE_GAP = 8.0  # écart maximal pour fusionner deux fragments numériques
# (les fragments de milliers OCR/type « 3 » + « 105,54 » sont espacés de
# quelques points ; deux colonnes adjacentes le sont de dizaines de points)


class OCRUnavailable(RuntimeError):
    """Levée quand tesseract ou pdftoppm n'est pas installé."""


def _run_ocr(data: bytes, page_index: int, dpi: int = 300):
    """OCR d'une page : retourne (mots, confiance moyenne)."""
    with tempfile.TemporaryDirectory() as td:
        pdf_path = os.path.join(td, 'doc.pdf')
        with open(pdf_path, 'wb') as f:
            f.write(data)
        r = subprocess.run(
            ['pdftoppm', '-r', str(dpi), '-png', '-f', str(page_index + 1),
             '-l', str(page_index + 1), pdf_path, os.path.join(td, 'p')],
            capture_output=True)
        pngs = sorted(f for f in os.listdir(td) if f.endswith('.png'))
        if r.returncode != 0 or not pngs:
            raise OCRUnavailable('pdftoppm a échoué ou est absent')
        png = os.path.join(td, pngs[0])
        if subprocess.run(['tesseract', '--version'], capture_output=True).returncode != 0:
            raise OCRUnavailable('tesseract est absent')
        tsv = subprocess.run(['tesseract', png, 'stdout', 'tsv'],
                             capture_output=True, text=True, timeout=120)
    words = []
    confs = []
    for row in (r.split('\t') for r in tsv.stdout.splitlines()[1:] if r.strip()):
        if len(row) < 12 or not row[11].strip():
            continue
        try:
            conf = float(row[10])
        except ValueError:
            continue
        if conf < 40:
            continue
        scale = 72.0 / dpi
        left, top, wd = float(row[6]) * scale, float(row[7]) * scale, float(row[8]) * scale
        words.append({'text': row[11].strip(), 'x0': left, 'x1': left + wd,
                      'top': top})
        confs.append(conf)
    avg_conf = round(sum(confs) / len(confs), 1) if confs else None
    return words, avg_conf


def _merge_numeric_fragments(tokens):
    """Fusionne « 1 » + « 234,56 » (séparés par l'espace des milliers)."""
    toks = sorted(tokens, key=lambda t: t[2])
    merged = []
    for t in toks:
        if (merged and NUM_RE.fullmatch(t[1]) and NUM_RE.fullmatch(merged[-1][1])
                and t[2] - merged[-1][3] < MERGE_GAP):
            _, prev_text, px0, px1, ptop = merged[-1]
            merged[-1] = (t[0], prev_text + ' ' + t[1], px0, t[3], ptop)
        else:
            merged.append(t)
    return merged


def extract_lines(data: bytes, ocr_dpi: int = 300):
    """Extrait les lignes de toutes les pages.

    Retourne (lines, page_infos, n_pages) où :
    - lines : liste de lignes, chaque ligne est une liste de tokens
      (page_index, text, x0, x1, top) triés de gauche à droite ;
    - page_infos : [{'method': 'text'|'ocr', 'ocr_confidence': float|None}]
    """
    import pdfplumber

    lines = []
    page_infos = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        n_pages = len(pdf.pages)
        for pi, page in enumerate(pdf.pages):
            ws = page.extract_words()
            method, ocr_conf = 'text', None
            if len(ws) < 5:
                try:
                    ws, ocr_conf = _run_ocr(data, pi, dpi=ocr_dpi)
                    method = 'ocr'
                except (OCRUnavailable, subprocess.TimeoutExpired):
                    ws = []
            page_infos.append({'method': method, 'ocr_confidence': ocr_conf})
            page_items = [(pi, w['text'], w['x0'], w['x1'], w['top']) for w in ws]
            page_items.sort(key=lambda t: (t[4], t[2]))
            cur = []
            for it in page_items:
                if cur and abs(it[4] - cur[0][4]) > LINE_TOL:
                    lines.append(_merge_numeric_fragments(cur))
                    cur = [it]
                else:
                    cur.append(it)
            if cur:
                lines.append(_merge_numeric_fragments(cur))
    return lines, page_infos, n_pages


def page_text(page) -> str:
    """Texte brut d'une page pdfplumber (recherche de devise, d'IBAN…)."""
    return page.extract_text() or ''
