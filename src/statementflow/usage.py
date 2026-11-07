"""Comptage d'usage et quotas — préparation de la facturation à l'usage.

AUCUNE passerelle de paiement n'est intégrée : ce module ne fait que
compter (pages et documents traités) et vérifier des quotas. Un
fournisseur de paiement pourra être branché plus tard sur ces compteurs
sans modifier le moteur.

Deux modes :
- local (défaut) : aucun stockage, simple journal en mémoire ;
- fichier JSON (STATEMENTFLOW_USAGE_STORE_PATH) : compteurs persistants
  par clé d'API, réinitialisables.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field


@dataclass
class UsageRecord:
    key: str
    documents: int = 0
    pages: int = 0
    transactions: int = 0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


class UsageStore:
    """Compteurs d'usage thread-safe, persistables en JSON."""

    def __init__(self, path: str = ''):
        self._path = path
        self._lock = threading.Lock()
        self._records: dict[str, UsageRecord] = {}
        self._load()

    def _load(self):
        if self._path and os.path.exists(self._path):
            try:
                with open(self._path, encoding='utf-8') as f:
                    data = json.load(f)
                self._records = {
                    k: UsageRecord(k, **v) for k, v in data.items()}
            except (json.JSONDecodeError, TypeError, OSError):
                self._records = {}

    def _flush(self):
        if self._path:
            tmp = self._path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({k: {
                    'documents': r.documents, 'pages': r.pages,
                    'transactions': r.transactions,
                    'first_seen': r.first_seen, 'last_seen': r.last_seen,
                } for k, r in self._records.items()}, f)
            os.replace(tmp, self._path)

    def record(self, key: str, pages: int, transactions: int):
        with self._lock:
            r = self._records.setdefault(key, UsageRecord(key))
            r.documents += 1
            r.pages += pages
            r.transactions += transactions
            r.last_seen = time.time()
            self._flush()

    def get(self, key: str) -> dict:
        with self._lock:
            r = self._records.get(key)
            if r is None:
                return {'documents': 0, 'pages': 0, 'transactions': 0}
            return {'documents': r.documents, 'pages': r.pages,
                    'transactions': r.transactions}

    def reset(self, key: str):
        with self._lock:
            self._records.pop(key, None)
            self._flush()


@dataclass
class Quota:
    """Quota mensuel simple (extensible : par fenêtre glissante, par plan…)."""
    max_documents: int | None = None
    max_pages: int | None = None

    def check(self, usage: dict) -> tuple[bool, str]:
        if self.max_documents is not None and usage['documents'] >= self.max_documents:
            return False, f'Quota de documents atteint ({self.max_documents})'
        if self.max_pages is not None and usage['pages'] >= self.max_pages:
            return False, f'Quota de pages atteint ({self.max_pages})'
        return True, ''
