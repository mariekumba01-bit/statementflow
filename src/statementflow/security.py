"""Sécurité API : clés d'API hashées, comparaison à temps constant, admin.

Modèle de sécurité (v0.2.0) :

- Toute route privée exige l'en-tête X-API-Key (401 si absent, 401 si
  inconnu). Le mode local reste sans compte : la CLI et le SDK local
  n'utilisent jamais ce module.
- Les clés valides sont configurées côté serveur sous forme de HASHES
  SHA-256 (variable STATEMENTFLOW_API_KEY_HASHES, hexadécimal, séparés
  par des virgules). La clé en clair n'est jamais stockée, journalisée
  ni renvoyée.
- Si aucun hash n'est configuré, le serveur démarre en « mode ouvert » :
  toute clé NON VIDE est acceptée (séparation par clé et quotas actifs).
  Ce mode est destiné au développement local ; il est signalé dans
  /health (`auth_mode`) et documenté dans docs/SECURITY.md.
- Les comparaisons de secrets passent par hmac.compare_digest (temps
  constant) — jamais par ==.
- Les opérations d'administration (consultation globale de l'usage,
  réinitialisation des compteurs) exigent une clé d'administration
  DISTINCTE (STATEMENTFLOW_ADMIN_KEY, hashée au chargement). Sans elle,
  les routes d'admin répondent 403.
- Génération et empreinte de clés : `statementflow keys create` (génère
  une clé aléatoire + son hash) et `statementflow keys hash` (hash d'une
  clé existante) — voir cli.py.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Optional

ENV_PREFIX = 'STATEMENTFLOW_'


def hash_key(key: str) -> str:
    """Empreinte SHA-256 d'une clé (format de stockage côté serveur)."""
    return hashlib.sha256(key.encode('utf-8')).hexdigest()


def mask_key(key: str) -> str:
    """Masque une clé pour tout affichage/réponse : sk_abcdefgh… → sk_ab****."""
    if not key:
        return ''
    return key[:4] + '****' if len(key) > 4 else '****'


def generate_key() -> str:
    """Clé d'API aléatoire (256 bits d'entropie), à afficher une seule fois.
    Préfixe « sf_ » garanti : une clé ne peut jamais commencer par « - »
    (sinon argparse la traiterait comme une option dans la CLI)."""
    return 'sf_' + secrets.token_urlsafe(32)


class ApiKeyStore:
    """Vérification des clés d'API contre une liste de hashes SHA-256."""

    def __init__(self, key_hashes: list[str] | tuple[str, ...] = ()):
        self._hashes = tuple(h.strip().lower() for h in key_hashes
                             if h and h.strip())

    @classmethod
    def from_env(cls) -> 'ApiKeyStore':
        raw = os.environ.get(ENV_PREFIX + 'API_KEY_HASHES', '')
        return cls(raw.split(',')) if raw else cls(())

    @property
    def configured(self) -> bool:
        """True si des hashes sont configurés (mode fermé)."""
        return bool(self._hashes)

    @property
    def mode(self) -> str:
        return 'key-hash-list' if self.configured else 'open'

    def verify(self, key: str | None) -> bool:
        """Clé valide ? Temps constant sur chaque comparaison de hash."""
        if not key:
            return False
        if not self.configured:
            # mode ouvert (développement local) : toute clé non vide est
            # acceptée ; la séparation par clé et les quotas restent actifs
            return True
        h = hash_key(key)
        return any(hmac.compare_digest(h, ref) for ref in self._hashes)


class AdminAuthorizer:
    """Autorisation administrative distincte (réinitialisations, audit)."""

    def __init__(self, admin_key_hash: str = ''):
        self._hash = (admin_key_hash or '').strip().lower()

    @classmethod
    def from_env(cls) -> 'AdminAuthorizer':
        raw = os.environ.get(ENV_PREFIX + 'ADMIN_KEY', '')
        # la clé admin en clair est hashée immédiatement puis perdue
        return cls(hash_key(raw)) if raw else cls()

    @property
    def configured(self) -> bool:
        return bool(self._hash)

    def verify(self, key: str | None) -> bool:
        if not key or not self.configured:
            return False
        return hmac.compare_digest(hash_key(key), self._hash)


def sanitize_filename(name: str | None, max_len: int = 200) -> str:
    """Nettoie un nom de fichier fourni par un client : chemin retiré
    (path traversal), caractères de contrôle supprimés, longueur plafonnée.
    Utilisé pour les journaux et les métadonnées — jamais pour accéder
    au système de fichiers (le traitement lit des octets en mémoire).
    """
    if not name:
        return 'document'
    name = os.path.basename(name.replace('\\', '/')).strip()
    name = ''.join(c for c in name if c.isprintable() and not c.isspace()
                   or c in ' ._-()')
    name = ' '.join(name.split())
    if not name or name in {'.', '..'}:
        return 'document'
    return name[:max_len]


class ManagedKey:
    """Clé API commerciale : hash + crédit prépayé + révocation.

    La clé en clair n'existe qu'au moment de l'émission (affichée une
    seule fois par la CLI) ; seuls le hash SHA-256 et les attributs
    commerciaux sont persistés."""

    def __init__(self, label: str, key_hash: str, credits: float = 0.0,
                 revoked: bool = False, created: str = ''):
        self.label = label
        self.key_hash = key_hash
        self.credits = float(credits)
        self.revoked = revoked
        self.created = created or time.strftime(
            '%Y-%m-%dT%H:%M:%SZ', time.gmtime())

    def to_dict(self) -> dict:
        return {'label': self.label, 'key_hash': self.key_hash,
                'credits': self.credits, 'revoked': self.revoked,
                'created': self.created}


class ManagedKeyStore:
    """Clés API gérées (JSON persistant) : émission, révocation, crédits.

    Utilisé par l'opérateur via la CLI (`statementflow keys issue|revoke|
    list|credits`) et par l'API (vérification + décompte de crédits).
    Le fichier est rechargé à la volée (mtime) : une clé émise par la CLI
    est utilisable immédiatement sans redémarrer l'API. Une clé peut aussi
    être déclarée dans STATEMENTFLOW_API_KEY_HASHES (sans crédits, non
    gérée) — les deux mécanismes coexistent."""

    def __init__(self, path: str = ''):
        self._path = path
        self._lock = threading.Lock()
        self._records: dict[str, ManagedKey] = {}
        self._mtime = 0.0
        self._load()

    @classmethod
    def from_env(cls) -> 'ManagedKeyStore':
        return cls(os.environ.get(ENV_PREFIX + 'KEYS_PATH', ''))

    def _load(self, force: bool = False):
        if not self._path or not os.path.exists(self._path):
            return
        mtime = os.path.getmtime(self._path)
        if not force and mtime == self._mtime:
            return
        try:
            with open(self._path, encoding='utf-8') as f:
                data = json.load(f)
            records = {h: ManagedKey(**r) for h, r in data.items()}
        except (json.JSONDecodeError, TypeError, OSError):
            return  # fichier illisible : on garde l'état précédent
        with self._lock:
            self._records = records
            self._mtime = mtime

    def _flush(self):
        if not self._path:
            return
        tmp = self._path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({h: k.to_dict() for h, k in self._records.items()},
                      f, ensure_ascii=False)
        os.replace(tmp, self._path)
        self._mtime = os.path.getmtime(self._path)

    def lookup(self, key_hash: str) -> Optional[ManagedKey]:
        """Clé gérée valide (présente, non révoquée) ou None.
        Relecture forcée : la granularité d'mtime du système de fichiers
        peut masquer une écriture récente (API et CLI en parallèle)."""
        self._load(force=True)
        with self._lock:
            k = self._records.get(key_hash)
            if k is None or k.revoked:
                return None
            return k

    def issue(self, label: str, credits: float = 0.0) -> tuple[str, ManagedKey]:
        """Émet une clé : retourne (clé_en_clair, enregistrement). La clé
        en clair n'est JAMAIS persistée — l'appelant l'affiche une fois."""
        key = generate_key()
        record = ManagedKey(label, hash_key(key), credits=credits)
        with self._lock:
            self._records[record.key_hash] = record
            self._flush()
        return key, record

    def revoke(self, key_hash: str) -> bool:
        self._load(force=True)
        with self._lock:
            k = self._records.get(key_hash)
            if k is None:
                return False
            k.revoked = True
            self._flush()
            return True

    def add_credits(self, key_hash: str, credits: float) -> bool:
        self._load(force=True)
        with self._lock:
            k = self._records.get(key_hash)
            if k is None:
                return False
            k.credits = max(0.0, k.credits + credits)
            self._flush()
            return True

    def deduct(self, key_hash: str, credits: float) -> float:
        """Débite des crédits (plancher 0). Retourne le solde restant.
        Recharge l'état disque avant mutation : deux processus (API +
        CLI) peuvent ajuster le même fichier."""
        self._load(force=True)
        with self._lock:
            k = self._records.get(key_hash)
            if k is None:
                return -1.0
            k.credits = max(0.0, k.credits - credits)
            self._flush()
            return k.credits

    def balance(self, key_hash: str) -> Optional[float]:
        self._load()
        with self._lock:
            k = self._records.get(key_hash)
            return None if k is None else k.credits

    def list(self) -> list[dict]:
        self._load()
        with self._lock:
            return [k.to_dict() for k in self._records.values()]
