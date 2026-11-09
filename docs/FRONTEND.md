# Application web StatementFlow (v0.5.0)

Produit commercial autonome servi par l'API elle-même — **aucun build,
aucune dépendance frontend** (HTML + CSS + JS vanilla dans `web/`).

URL de production : https://statementflow-api-ch0f.onrender.com/

## Écrans

| URL | Rôle |
|---|---|
| `/` | Landing commerciale (SEO, Open Graph, tarifs, FAQ, cas d'usage) |
| `/demo.html` | Démo interactive PUBLIQUE : vrai moteur, sans compte, sans débit (2 Mo, 6/h/IP) |
| `/signup.html` | Inscription (email + mot de passe, crédits d'accueil offerts) |
| `/login.html` | Connexion |
| `/verify.html?token=…` | Vérification d'email (token usage unique) |
| `/forgot.html` / `/reset.html?token=…` | Réinitialisation de mot de passe |
| `/dashboard.html` | Tableau de bord client (private) |
| `/admin.html` | Statistiques administrateur (clé X-Admin-Key) |
| `/api.html` | Documentation API + exemples Python / JavaScript / n8n |
| `/legal.html` | Mentions légales, confidentialité, CGV |
| `/offline.html` | Écran hors-ligne (service worker) |

## Architecture

- **Backend** : `src/statementflow/webapp.py` — routes `/v1/auth/*`,
  `/v1/account/*`, `/v1/track`, `/v1/admin/stats`, `/v1/demo/analyze`.
  Monté dans `api.py` APRÈS toutes les routes API ; le frontend statique
  (`web/`) est servi en dernier recours (`mount_webapp`) ;
- **Comptes** : PostgreSQL (migration 005) — mots de passe PBKDF2,
  sessions par cookie HttpOnly (token haché en base), tokens d'usage
  unique, isolation stricte par `user_id` ;
- **Frontend** : `web/app.js` (fetch vers l'API same-origin, aucun
  secret côté navigateur), `web/style.css` (mobile-first), PWA
  (`manifest.webmanifest`, `sw.js` cache **statique uniquement** —
  jamais `/v1/*`).

## Parcours d'achat (SasPay)

1. Le client choisit un pack (Starter/Pro/Business) ;
2. `POST /v1/account/topup` → le serveur calcule le montant depuis le
   catalogue (jamais le navigateur) et crée la session SasPay ;
3. Redirection vers la page de paiement SasPay ;
4. Au retour sur `/dashboard.html`, le frontend sonde
   `GET /v1/account/topup/{reference}` : le serveur interroge le statut
   RÉEL chez SasPay et active les crédits si le paiement est confirmé ;
5. Le webhook `POST /v1/billing/webhook` (signature HMAC) fait de
   même — l'activation est idempotente (transaction_id unique) : le
   webhook ET le polling peuvent tous deux activer sans double crédit.

Sans `STATEMENTFLOW_SASPAY_SECRET_KEY` (mode test), la passerelle est
un simulateur local : **aucun paiement réel n'est possible** et le
tableau de bord le montre via les statuts de session de test.

## Tests

`tests/test_webapp.py` (22 tests, PostgreSQL réel) : inscription,
session, isolation entre comptes, conversion réelle + débit/remboursement,
rotation de clé, 3 packs, cycle paiement (webhook signé, rejeu sans
double crédit, polling sans webhook, échec), statistiques admin
(CA = rechargements activés uniquement), démo publique, frontend servi.

## Variables d'environnement (aucune obligatoire pour démarrer)

| Variable | Effet |
|---|---|
| `STATEMENTFLOW_DATABASE_URL` | PostgreSQL — **requis pour les comptes** (sinon 503 explicite sur /v1/auth/*) |
| `STATEMENTFLOW_SIGNUP_BONUS_CREDITS` | Crédits d'accueil (défaut 5 ; 0 = désactivé) |
| `STATEMENTFLOW_SMTP_HOST/_PORT/_USER/_PASSWORD/_FROM` | Envoi des emails de vérification/reset (sans SMTP : compte immédiatement utilisable, `verified_via=auto_no_smtp`) |
| `STATEMENTFLOW_SASPAY_SECRET_KEY` / `_WEBHOOK_SECRET` | Passerelle réelle (sinon simulateur de test) |
| `STATEMENTFLOW_ADMIN_KEY` | Accès /v1/admin/stats et /admin.html |
| `STATEMENTFLOW_PUBLIC_BASE_URL` | URL des liens d'email si différente de l'origine de la requête |

## Sitemap SEO

`/robots.txt` et `/sitemap.xml` sont générés avec l'URL de production ;
les pages privées (dashboard, admin) sont `noindex`.
