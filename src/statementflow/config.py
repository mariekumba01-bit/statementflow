"""Configuration du moteur : limites de traitement et options.

Tout est réglable par variables d'environnement (préfixe STATEMENTFLOW_),
aucune valeur sensible n'est stockée en dur.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

ENV_PREFIX = 'STATEMENTFLOW_'


@dataclass
class Config:
    # limites de traitement (protection contre les fichiers malveillants)
    max_file_mb: float = 20.0
    max_pages: int = 200
    max_rows: int = 200_000
    # options moteur
    ocr_dpi: int = 300
    tolerance: float = 0.01  # tolérance des contrôles de cohérence (unité monétaire)
    # API (utilisées par statementflow.api)
    api_host: str = '127.0.0.1'
    api_port: int = 8080
    results_ttl_s: int = 900  # durée de rétention des résultats en mémoire
    # quotas par clé d'API (None = pas de limite ; v0.2.0, câblés à l'API)
    quota_documents: int | None = None
    quota_pages: int | None = None
    # comptage d'usage (voir usage.py) — désactivé par défaut en local
    usage_store_path: str = ''

    @classmethod
    def from_env(cls) -> 'Config':
        def env(name, default, cast):
            raw = os.environ.get(ENV_PREFIX + name)
            if raw is None or raw == '':
                return default
            try:
                return cast(raw)
            except ValueError:
                return default

        def env_opt(name, cast):
            raw = os.environ.get(ENV_PREFIX + name)
            if raw is None or raw == '':
                return None
            try:
                return cast(raw)
            except ValueError:
                return None

        return cls(
            max_file_mb=env('MAX_FILE_MB', 20.0, float),
            max_pages=env('MAX_PAGES', 200, int),
            max_rows=env('MAX_ROWS', 200_000, int),
            ocr_dpi=env('OCR_DPI', 300, int),
            tolerance=env('TOLERANCE', 0.01, float),
            api_host=env('API_HOST', '127.0.0.1', str),
            api_port=env('API_PORT', 8080, int),
            results_ttl_s=env('RESULTS_TTL_S', 900, int),
            quota_documents=env_opt('QUOTA_DOCUMENTS', int),
            quota_pages=env_opt('QUOTA_PAGES', int),
            usage_store_path=env('USAGE_STORE_PATH', '', str),
        )
