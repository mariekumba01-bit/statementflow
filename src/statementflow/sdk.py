"""SDK Python client — accès pratique au moteur et à l'API StatementFlow.

Deux modes :
- local (défaut) : appelle le moteur directement dans le processus ;
- distant : interroge une API StatementFlow (client HTTP minimal,
  stdlib uniquement).

Exemple :
    from statementflow.sdk import Client
    c = Client()
    r = c.parse('releve.pdf')
    print(r.totals, r.checks)
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Optional


class StatementFlowResult:
    """Enveloppe du résultat : accès attribut + export CSV."""

    def __init__(self, data: dict):
        self.data = data

    def __getattr__(self, name):
        try:
            return self.data[name]
        except KeyError as e:
            raise AttributeError(name) from e

    @property
    def transactions(self) -> list[dict]:
        return self.data['transactions']

    @property
    def is_balanced(self) -> Optional[bool]:
        return self.data['checks']['opening_closing_ok']

    def to_json(self, path: str) -> None:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def to_csv(self, path: str, sep: str = ';') -> None:
        cols = ['index', 'transaction_id', 'date', 'description', 'label',
                'debit', 'credit', 'amount', 'balance', 'currency',
                'confidence']
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(sep.join(cols) + '\n')
            for t in self.transactions:
                f.write(sep.join(
                    '' if t.get(c) is None else str(t.get(c)) for c in cols)
                    + '\n')


class Client:
    """Client StatementFlow (local ou distant)."""

    def __init__(self, base_url: str = '', api_key: str = '',
                 currency_hint: str = '', timeout: int = 120):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key or os.environ.get('STATEMENTFLOW_API_KEY', '')
        self.currency_hint = currency_hint
        self.timeout = timeout

    def parse(self, path: str, currency_hint: Optional[str] = None
              ) -> StatementFlowResult:
        """Analyse un fichier local (moteur local ou upload vers l'API)."""
        hint = currency_hint if currency_hint is not None else self.currency_hint
        if self.base_url:
            return self._parse_remote(path, hint)
        from .engine import parse_file
        return StatementFlowResult(parse_file(path, currency_hint=hint or None))

    def parse_bytes(self, data: bytes, filename: str,
                    currency_hint: Optional[str] = None
                    ) -> StatementFlowResult:
        hint = currency_hint if currency_hint is not None else self.currency_hint
        if self.base_url:
            raise NotImplementedError(
                'parse_bytes distant : utilisez parse(path) (upload fichier)')
        from .engine import parse_bytes
        return StatementFlowResult(
            parse_bytes(data, filename, currency_hint=hint or None))

    # ── mode distant (API) ──────────────────────────────────────────
    def _parse_remote(self, path: str, hint: str) -> StatementFlowResult:
        import urllib.error
        import uuid

        boundary = uuid.uuid4().hex
        filename = os.path.basename(path)
        with open(path, 'rb') as f:
            payload = f.read()
        body = b''.join([
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\n\r\n'.encode(),
            payload, f'\r\n--{boundary}--\r\n'.encode()])
        headers = {'Content-Type': f'multipart/form-data; boundary={boundary}'}
        if self.api_key:
            headers['X-API-Key'] = self.api_key
        if hint:
            headers['X-Currency-Hint'] = hint
        req = urllib.request.Request(
            self.base_url + '/v1/analyze', data=body, headers=headers,
            method='POST')
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                job = json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')
            if e.code == 401:
                raise RuntimeError(
                    'API 401 : clé d\'API absente ou invalide — fournissez '
                    'api_key=… ou la variable d\'environnement '
                    'STATEMENTFLOW_API_KEY.') from None
            raise RuntimeError(f'API {e.code} : {detail}') from None
        # polling du résultat
        import time
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            req = urllib.request.Request(
                self.base_url + '/v1/results/' + job['job_id'])
            if self.api_key:
                req.add_header('X-API-Key', self.api_key)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.load(resp)
            if data.get('status') == 'done':
                return StatementFlowResult(data)
            if data.get('status') == 'error':
                raise RuntimeError(f"Échec d'analyse : {data.get('error')}")
            time.sleep(0.3)
        raise TimeoutError('Résultat non disponible avant expiration.')

    def usage(self) -> dict:
        """Compteurs d'usage de la clé (mode distant uniquement)."""
        if not self.base_url:
            raise RuntimeError(
                'usage() nécessite le mode distant : Client(base_url=…, '
                'api_key=…). En mode local, le moteur ne comptabilise rien.')
        req = urllib.request.Request(self.base_url + '/v1/usage')
        if self.api_key:
            req.add_header('X-API-Key', self.api_key)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.load(resp)
