"""Adaptateurs de formats : point d'extension pour ajouter une banque.

PRINCIPE : le moteur central est générique (détection de colonnes par
clustering, montants/dates multiformats). Un adaptateur n'est PAS un
parser dédié : c'est une **configuration** (indices de détection +
surcharges) enregistrée sous un nom. Quand un nouveau format bancaire
résiste au moteur générique, on ajoute un adaptateur ici sans toucher
au cœur.

Pour ajouter un format :
1. créer un Adapter avec une fonction detect() (une ou deux lignes de
   règles sur l'entête / le texte du document) ;
2. l'enregistrer dans DEFAULT_ADAPTERS ;
3. fournir les surcharges utiles (synonymes d'entête, devise par défaut
   SI le format la garantit toujours, format de date imposé…) ;
4. ajouter des tests de non-régression dans tests/.

Les adaptateurs fournis ci-dessous sont volontairement minces : le moteur
générique couvre déjà ces formats sur le corpus de test. Ils servent de
modèle d'extension documenté.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class Adapter:
    name: str
    kind: str  # 'pdf' | 'tabular' | 'any'
    detect: Callable[[str, list], bool]  # (texte_aplati, premières_lignes) -> bool
    description: str = ''
    # surcharges optionnelles
    extra_header_synonyms: dict = field(default_factory=dict)
    currency_hint: Optional[str] = None  # uniquement si le format la garantit TOUJOURS
    notes: str = ''


def _first_lines_text(rows: list, n: int = 5) -> str:
    return ' | '.join(' '.join(str(c) for c in r) for r in rows[:n])


DEFAULT_ADAPTERS: list[Adapter] = [
    Adapter(
        name='wave-csv',
        kind='tabular',
        detect=lambda text, rows: bool(
            re.search(r'Montant\s*\((FCFA|XOF)\)', _first_lines_text(rows), re.I)),
        description='Export CSV Wave / Wave Business (FCFA entiers, colonne Sens).',
        currency_hint=None,  # la devise est lisible dans l'entête, pas de forçage
        notes='Exemple de modèle : le moteur générique gère ce format sans surcharge.',
    ),
    Adapter(
        name='orange-money-csv',
        kind='tabular',
        detect=lambda text, rows: bool(
            re.search(r'\b(RECEPTION|ENVOI)\b', _first_lines_text(rows))),
        description='Export Orange Money (montant signé, dates ISO).',
    ),
    Adapter(
        name='mtn-momo-csv',
        kind='tabular',
        detect=lambda text, rows: bool(
            re.search(r'Transaction\s*ID', _first_lines_text(rows), re.I)),
        description='Export MTN MoMo (Amount signé, identifiant de transaction).',
    ),
    Adapter(
        name='generic-pdf',
        kind='pdf',
        detect=lambda text, rows: True,
        description='Relevé PDF générique (détection de colonnes par clustering).',
        notes='Adaptateur par défaut : aucune surcharge, moteur générique intégral.',
    ),
]


def detect_adapter(kind: str, text: str, rows: list) -> Adapter:
    """Premier adaptateur dont la détection matche (ordre d'inscription)."""
    for adapter in DEFAULT_ADAPTERS:
        if adapter.kind in (kind, 'any'):
            try:
                if adapter.detect(text, rows):
                    return adapter
            except Exception:
                continue
    return DEFAULT_ADAPTERS[-1]
