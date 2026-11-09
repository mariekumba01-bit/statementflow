"""Comptes clients : inscription, connexion, sessions, vérification
email, réinitialisation de mot de passe (v0.5.0).

Architecture — cohérente avec le reste du produit :
- mots de passe : PBKDF2-HMAC-SHA256 (stdlib, 600 000 itérations par
  défaut — recommandation OWASP 2023 ; sel aléatoire de 16 octets par
  compte). Le nombre d'itérations est ajustable pour les tests
  (STATEMENTFLOW_PBKDF2_ITERATIONS) ;
- sessions navigateur : token aléatoire 256 bits, stocké HACHÉ
  (SHA-256) dans PostgreSQL, cookie HttpOnly/SameSite=Lax (Secure
  détecté par le schéma de la requête) — le token en clair ne vit que
  dans le cookie ;
- tokens à usage unique : vérification d'email et reset de mot de
  passe, hachés, expiration courte, usage UNIQUE (consommation
  atomique) ;
- vérification d'email : lien signé envoyé par SMTP si configuré
  (STATEMENTFLOW_SMTP_*). Sans SMTP, le compte est utilisable
  immédiatement avec verified_via='auto_no_smtp' — signalé
  honnêtement dans l'interface et /health-like (aucune fausse
  prétention de vérification) ;
- envoi d'emails : smtplib (stdlib), best effort, aucun secret
  journalisé ;
- attribution marketing : ?ref= prioritaire, sinon classification du
  referrer (direct/google/linkedin/facebook/twitter/github/campaigne
  utm_source) — enregistrée à l'inscription.

Aucune donnée bancaire ne transite par ce module.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from typing import Optional

ENV_PREFIX = 'STATEMENTFLOW_'

SESSION_TTL_S = 30 * 24 * 3600        # 30 jours
VERIFY_TOKEN_TTL_S = 48 * 3600        # 48 heures
RESET_TOKEN_TTL_S = 3600              # 1 heure
MIN_PASSWORD_LEN = 10
DEFAULT_PBKDF2_ITERATIONS = 600_000

_EMAIL_RE = re.compile(
    r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$')

# domaines jetables refusés à l'inscription (anti-spam basique ; la
# liste reste volontairement courte : les faux positifs coûtent des
# clients réels)
DISPOSABLE_DOMAINS = {
    'yopmail.com', 'tempmail.com', 'temp-mail.org', 'guerrillamail.com',
    'mailinator.com', '10minutemail.com', 'throwawaymail.com',
    'sharklasers.com', 'getnada.com', 'dispostable.com',
}

_REFERRER_SOURCES = (
    ('google.', 'google'), ('bing.com', 'bing'), ('duckduckgo.', 'duckduckgo'),
    ('linkedin.', 'linkedin'), ('facebook.', 'facebook'),
    ('instagram.', 'instagram'), ('twitter.', 'twitter'), ('x.com', 'twitter'),
    ('t.co', 'twitter'), ('github.com', 'github'), ('youtube.', 'youtube'),
    ('whatsapp.', 'whatsapp'),
)


class AccountError(ValueError):
    """Erreur de validation de compte (message affichable, sans secret)."""


# ── mots de passe ────────────────────────────────────────────────────
def pbkdf2_iterations() -> int:
    try:
        return max(1000, int(os.environ.get(
            ENV_PREFIX + 'PBKDF2_ITERATIONS', DEFAULT_PBKDF2_ITERATIONS)))
    except (TypeError, ValueError):
        return DEFAULT_PBKDF2_ITERATIONS


def hash_password(password: str, salt: Optional[bytes] = None) -> tuple[str, str]:
    """(hash_hex, salt_hex) — PBKDF2-HMAC-SHA256, sel 16 octets."""
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(
        'sha256', password.encode('utf-8'), salt, pbkdf2_iterations())
    return dk.hex(), salt.hex()


def verify_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    """Vérifie un mot de passe (temps quasi constant par construction)."""
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac(
        'sha256', password.encode('utf-8'), salt, pbkdf2_iterations())
    return hmac.compare_digest(dk, expected)


def validate_email(email: str) -> str:
    """Email normalisé (minuscules, trim) ou AccountError."""
    email = (email or '').strip().lower()
    if not email or len(email) > 254 or not _EMAIL_RE.match(email):
        raise AccountError('Adresse email invalide.')
    domain = email.rsplit('@', 1)[1]
    if domain in DISPOSABLE_DOMAINS:
        raise AccountError(
            'Les adresses email temporaires ne sont pas acceptées '
            '(créez un compte avec une adresse durable).')
    return email


def validate_password(password: str) -> str:
    """Mot de passe acceptable ou AccountError (≥ 10 caractères)."""
    if not password or len(password) < MIN_PASSWORD_LEN:
        raise AccountError(
            f'Mot de passe trop court : {MIN_PASSWORD_LEN} caractères '
            'minimum.')
    if len(password) > 1024:
        raise AccountError('Mot de passe trop long.')
    if password.strip() != password or not password.strip():
        raise AccountError(
            'Le mot de passe ne doit pas commencer/finir par une espace.')
    return password


# ── tokens & sessions ────────────────────────────────────────────────
def new_token() -> tuple[str, str]:
    """(token_clair, token_hash) — le clair n'est jamais stocké."""
    token = 'sf_' + secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def classify_source(referrer: str, ref_param: str) -> str:
    """Attribution marketing : ?ref= prioritaire, sinon referrer.

    Valeurs : direct | google | bing | duckduckgo | linkedin | facebook |
    instagram | twitter | github | youtube | whatsapp | ref:… | referrer
    (tout autre site)."""
    ref_param = (ref_param or '').strip().lower()
    if ref_param:
        return 'ref:' + re.sub(r'[^a-z0-9_.-]', '', ref_param)[:60]
    referrer = (referrer or '').strip().lower()
    if not referrer:
        return 'direct'
    for marker, name in _REFERRER_SOURCES:
        if marker in referrer:
            return name
    try:
        host = referrer.split('//', 1)[-1].split('/', 1)[0]
        return 'site:' + host[:60]
    except Exception:
        return 'direct'


# ── email sortant (SMTP optionnel) ───────────────────────────────────
def smtp_configured() -> bool:
    return bool(os.environ.get(ENV_PREFIX + 'SMTP_HOST', '').strip())


def send_email(to_email: str, subject: str, body: str) -> bool:
    """Envoi best effort via SMTP (STATEMENTFLOW_SMTP_*). Retourne
    False sans lever : l'inscription ne doit jamais échouer pour un
    problème d'email. Aucun secret dans les messages d'erreur."""
    host = os.environ.get(ENV_PREFIX + 'SMTP_HOST', '').strip()
    if not host:
        return False
    port = int(os.environ.get(ENV_PREFIX + 'SMTP_PORT', '587') or 587)
    user = os.environ.get(ENV_PREFIX + 'SMTP_USER', '').strip()
    password = os.environ.get(ENV_PREFIX + 'SMTP_PASSWORD', '')
    sender = os.environ.get(ENV_PREFIX + 'SMTP_FROM', user or
                            'no-reply@statementflow.app')
    sender_name = os.environ.get(ENV_PREFIX + 'SMTP_FROM_NAME',
                                 'StatementFlow')
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = formataddr((sender_name, sender))
    msg['To'] = to_email
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            if os.environ.get(ENV_PREFIX + 'SMTP_TLS', '1') != '0':
                smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
        return True
    except Exception:
        return False


def verification_email_body(link: str) -> str:
    return (
        'Bienvenue sur StatementFlow !\n\n'
        'Confirmez votre adresse email en ouvrant ce lien (valable '
        f'48 heures) :\n\n{link}\n\n'
        'Si vous n\'êtes pas à l\'origine de cette inscription, '
        'ignorez simplement ce message.\n\n'
        '— L\'équipe StatementFlow')


def reset_email_body(link: str) -> str:
    return (
        'Réinitialisation de votre mot de passe StatementFlow\n\n'
        'Ouvrez ce lien pour choisir un nouveau mot de passe (valable '
        f'1 heure, usage unique) :\n\n{link}\n\n'
        'Si vous n\'avez rien demandé, ignorez ce message : votre mot '
        'de passe actuel reste inchangé.\n\n'
        '— L\'équipe StatementFlow')
