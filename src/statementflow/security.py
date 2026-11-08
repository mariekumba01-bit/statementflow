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
import os
import secrets

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
    """Clé d'API aléatoire (256 bits d'entropie), à afficher une seule fois."""
    return secrets.token_urlsafe(32)


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
