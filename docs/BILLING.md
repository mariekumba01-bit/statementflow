# Facturation — crédits prépayés, SasPay, intégrité financière

**État (v0.4.0) : livré et testé.** Le cycle complet existe dans le
code : achat d'un pack → paiement SasPay (ou simulateur) → webhook
signé → re-vérification serveur → activation transactionnelle →
crédits disponibles. Ce document décrit l'implémentation RÉELLE.

## Offre (docs/PRICING.md)

| Pack | Crédits | Prix |
|---|---|---|
| Starter | 500 | 5 000 XOF |
| Pro | 2 500 | 20 000 XOF |
| Business | 10 000 | 60 000 XOF |

**Unité de consommation** : 1 crédit = 1 page PDF, ou 1 tranche de
50 lignes d'un document tabulaire (CSV/TSV/XLSX). Depuis la v0.4.0 la
consommation compte les **lignes réelles** du document
(`usage.rows`, en-tête inclus — 31 lignes → 1 crédit, 121 → 3),
indépendamment du nombre de transactions reconnues. Un traitement
consomme au minimum 1 crédit ; un traitement **échoué n'en consomme
aucun** (réservation restituée).

## Cycle de paiement (implémenté)

1. `POST /v1/billing/topup` (clé du client) : le SERVEUR crée la
   session chez SasPay (`POST /checkout-sessions/`, montant issu du
   catalogue serveur `CREDIT_PACKS` — jamais du client) et enregistre
   le rechargement `pending` ;
2. le client paie sur `checkout_url` ;
3. SasPay appelle `POST /v1/billing/webhook`
   (`transaction.success`/`transaction.failed`) : signature
   **HMAC-SHA256** sur `{timestamp}.{corps brut}` (en-têtes
   `X-Webhook-Signature`/`-Timestamp`/`-Event`), fraîcheur ≤ 300 s,
   comparaison en temps constant ;
4. **re-vérification serveur obligatoire** : le webhook n'est JAMAIS
   cru seul — `GET /payments/{id}/verify/` est interrogé avant toute
   attribution ; un statut PENDING/unknown ne crédite pas (le polling
   tranchera) ;
5. activation **transactionnelle** (PostgreSQL : fonction
   `activate_topup` — marque le rechargement ET crédite le grand
   livre dans UNE transaction ; JSON : activation puis crédit) ;
6. idempotence triple : statut `pending` requis, `transaction_id`
   unique, mouvement de grand livre unique par référence — **un
   paiement rejoué ne peut jamais créditer deux fois** ;
7. échec/annulation → `failed` (aucun crédit) ; le client peut
   réessayer ;
8. filets de sécurité : polling client
   (`GET /v1/billing/status/{ref}` — statut réel interrogé à chaque
   appel) et **cron horaire** de réconciliation (rattrape tout
   webhook manqué de plus d'1 h).

## Débit des crédits (implémenté)

- **Réservation atomique** de 1 crédit avant traitement (`402` si
  solde insuffisant) ;
- complément au coût réel après succès (`settle`), **plafonné au
  solde disponible : jamais de solde négatif** ;
- **restitution intégrale sur échec** (`refund`) — un document
  corrompu ne consomme rien ;
- idempotence par `job_id` : réserve/settle/refund d'un même job ne
  se rejouent pas (contrainte `UNIQUE` PostgreSQL) ;
- montants TOUJOURS calculés côté serveur (`credits_for`) ;
- PostgreSQL : solde **dérivé du grand livre immuable**
  (`api_key_balances`), chaque mouvement porte sa nature
  (`topup`/`reserve`/`settle`/`refund`/`adjustment`) — historique
  financier complet et auditable.

## Réconciliation et intégrité

- Cron (`docs/CRON.md`) : compare rechargements activés ↔ mouvements
  de grand livre, détecte crédits sans paiement valide et soldes
  négatifs — **signale sans corriger** (la correction financière est
  humaine, procédure dans `docs/DEPLOYMENT.md`) ;
- `scripts/finance_simulation.py` : simulation de marge (usage
  opérateur, aucune donnée réelle).

## Mode test (sans compte marchand)

Sans `STATEMENTFLOW_SASPAY_SECRET_KEY` : `FakeSasPay` simule la
passerelle avec les MÊMES contrats (session, statut, vérification,
webhooks signés selon le schéma officiel). La route
`POST /v1/billing/_test/pay` (doublement verrouillée : mode test +
`STATEMENTFLOW_TEST_PAYMENT=1` ; **404 garanti en production**)
déclenche un paiement simulé et retourne le webhook signé à poster.
Le parcours complet est couvert par les tests automatisés (suite
`test_billing.py` + `test_pg.py`).

## Ce qui reste une action humaine

- Créer le compte marchand SasPay (app.saspay.me, KYC), récupérer
  `sk_live_…` et le secret webhook, les définir dans Render ;
- déclarer l'URL du webhook dans le dashboard SasPay ;
- conformité fiscale/TVa selon la juridiction.
