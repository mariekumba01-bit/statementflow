# API StatementFlow — référence

## Schéma JSON de sortie (statementflow/v1)

Le schéma est **versionné et stable** : les champs listés ici existent tous
depuis v0.1.0 sauf mention « v0.2.0 » (ajouts strictement rétrocompatibles —
aucun champ retiré ni renommé). Les valeurs inconnues restent `null` :
une valeur inconnue n'est jamais transformée en 0 et une transaction
n'est jamais fabriquée.

```jsonc
{
  "schema": "statementflow/v1",
  "document": {
    "document_id": "a1b2c3d4e5f60718",   // v0.2.0 : SHA-256 tronqué du contenu (stable)
    "schema_version": "statementflow/v1",// v0.2.0
    "filename": "releve.pdf",            // nom nettoyé (chemin et contrôles retirés)
    "kind": "pdf | csv | tsv | xlsx",
    "pages": 1,
    "method": "text | ocr | mixed | tabular",
    "parser": "generic-columns-v3",
    "adapter": "generic-pdf | wave-csv | ...",
    "statement_period": {"start": "2026-01-01", "end": "2026-01-31"}, // v0.2.0, null si aucune date
    "account_currency": "EUR"            // v0.2.0, alias de currency
  },
  "metadata": {                          // v0.2.0 : règles déterministes appliquées
    "date_interpretation": "JOUR/MOIS (Europe/UEMOA) pour les formats non ISO ; ISO YYYY-MM-DD accepté tel quel",
    "confidence_rule": "heuristique documentée (texte 1.0, OCR 0.75, OCR faible 0.6 ; −0.25 par donnée manquante ; plafond 0.5 si alerte d'erreur) — non calibrée"
  },
  "currency": "EUR | XOF | GBP | USD | null",   // null si non identifiable
  "opening_balance": 3812.53,                    // null si absent
  "closing_balance": -3319.45,
  "closing_derived": true,   // false = lu dans le document, true = déduit de la dernière ligne
  "transactions": [
    {
      "index": 1,
      "transaction_id": "9f8e7d6c5b4a3210",  // v0.2.0 : stable (hash document+index+données)
      "date": "2026-01-01",          // ISO 8601 ; date impossible (31/02…) → null + alerte
      "description": "CARTE X4921 AUCHAN",          // v0.2.0 : libellé normalisé
      "original_description": "CARTE X4921 AUCHAN", // v0.2.0 : libellé source
      "label": "CARTE X4921 AUCHAN", // compat v0.1 (== description)
      "debit": 82.40,                 // jamais négatif ; null si crédit
      "credit": null,                 // jamais négatif ; null si débit
      "amount": -82.40,               // v0.2.0 : signé (crédit − débit) ; null si aucun
      "balance": 2953.18,             // solde après opération, null si absent
      "currency": "EUR",              // devise de la transaction (colonne) ou du document
      "confidence": 1.0,              // 0.1–1.0, abaissée par les anomalies
      "alerts": ["date_unparseable"], // v0.2.0 : codes d'alerte de cette transaction
      "trace": {                      // traçabilité de l'extraction
        "page": 1,
        "method": "text | ocr | csv-row",
        "source_row": 7               // ligne (texte) ou index de ligne (CSV)
      }
    }
  ],
  "totals": {"count": 14, "debits": 7507.20, "credits": 6375.22},
  "checks": {
    "tolerance": 0.01,
    "chain_checked": 14,        // lignes où la chaîne des soldes est vérifiable
    "chain_matches": 14,
    "chain_breaks": [           // détails des ruptures
      {"transaction_index": 3, "expected_balance": 100.0, "actual_balance": 90.0}
    ],
    "chain_ok": true,           // null si aucune ligne vérifiable
    "opening_closing_ok": true, // ouverture + crédits − débits = clôture ; null si indisponible
    "date_order_ok": true,      // v0.2.0 : chronologie des dates ; null si < 2 dates
    "transactions_without_date": 0,    // v0.2.0
    "transactions_without_amount": 0,  // v0.2.0
    "totals": {"count": 14, "debits": 7507.2, "credits": 6375.22}
  },
  "validation": {               // v0.2.0 : statut global en 4 niveaux
    "status": "validated | warning | inconsistent | unverifiable",
    "verifiable": true,         // false = aucun contrôle possible (pas de soldes)
    "errors": 0,
    "warnings": 0
  },
  "alerts": [
    {
      "code": "date_unparseable",
      "severity": "info | warning | error",
      "message": "…",
      "transaction_index": 3     // null si l'alerte porte sur le document
    }
  ],
  "confidence": 1.0,             // confiance document, plafonnée à 0.5 si erreur
  "usage": {"pages": 1, "transactions": 14}   // compteurs (préparation facturation)
}
```

## Codes d'alerte

| Code | Sévérité | Signification |
|---|---|---|
| `ocr_used` | info | OCR appliqué aux pages sans couche texte |
| `ocr_low_confidence` | warning | confiance OCR < 80/100 sur une page |
| `page_header_skipped` | info | entête de continuation « (suite) » ignorée |
| `opening_missing` / `closing_missing` | info | solde d'ouverture/clôture absent du document |
| `currency_not_identified` | warning | aucune devise identifiable (null) — fournir `currency_hint` |
| `currency_mismatch` | warning | devise d'une ligne contredit la devise du document (v0.2.0) |
| `date_unparseable` | error | date non interprétable OU impossible (31/02, 29/02 non bissextile) → `date: null` |
| `undated_amount_line` | warning | ligne avec montants mais sans date |
| `date_order_inconsistent` | warning | dates non chronologiques (v0.2.0) |
| `amount_missing` | warning | ligne datée sans montant reconnu |
| `debit_and_credit_ambiguous` | warning | débit ET crédit reconnus sur la même ligne (v0.2.0) |
| `side_assumed_debit` | warning | montant positif sans signal de direction → débit supposé |
| `possible_duplicate` | warning | doublon adjacent conservé (cas légitime possible) |
| `duplicate_page_repeat` | info | répétition de page supprimée (conservée une fois) |
| `chain_break` | error | solde incohérent ligne à ligne |
| `unbalanced_statement` | error | ouverture + crédits − débits ≠ clôture |

## Endpoints REST

| Méthode | Chemin | Auth | Description |
|---|---|---|---|
| GET | `/health` | public | état, version, mode d'authentification, limites |
| POST | `/v1/analyze` | X-API-Key | soumet un document (multipart `file`) → `job_id` (202) |
| GET | `/v1/results/{job_id}` | X-API-Key | résultat ; **liée à la clé soumettrice** (404 sinon) |
| GET | `/v1/usage` | X-API-Key | compteurs de la clé appelante (clé masquée + hash) |
| GET | `/v1/admin/usage` | X-Admin-Key | usage global (hashes uniquement) |
| POST | `/v1/admin/usage/reset` | X-Admin-Key | réinitialise un compteur (`{"key_hash": "…"}`) |
| GET | `/v1/billing/packs` | public | catalogue des packs de crédits |
| POST | `/v1/billing/topup` | X-API-Key | démarre l'achat d'un pack → URL de paiement |
| GET | `/v1/billing/status/{reference}` | X-API-Key | statut d'un rechargement (interroge l'état réel) |
| POST | `/v1/billing/webhook` | signature SasPay | webhook d'encaissement (HMAC obligatoire) |
| GET | `/openapi.json`, `/docs` | public | spécification et documentation interactive |

### Authentification (obligatoire depuis v0.2.0, verrouillée par défaut depuis v0.3.1)

- Toute route privée exige l'en-tête `X-API-Key` : **401** si absent ou
  vide, **401** si inconnu quand des clés sont configurées.
- Clés valides : `STATEMENTFLOW_API_KEY_HASHES` (hashes SHA-256 séparés par
  des virgules) ou clés gérées via `STATEMENTFLOW_KEYS_PATH`
  (`statementflow keys issue`). Génération : `statementflow keys create` —
  la clé en clair n'est stockée ni journalisée ni renvoyée (réponses
  masquées).
- **Sans AUCUNE clé configurée**, les routes privées répondent **503**
  (`auth_mode: "locked"` dans `/health`) : l'API n'est jamais accessible
  sans authentification. Le mode ouvert (toute clé non vide acceptée,
  développement local uniquement) s'active explicitement par
  `STATEMENTFLOW_ALLOW_OPEN_AUTH=1` (`auth_mode: "open"`).
- Administration distincte : `STATEMENTFLOW_ADMIN_KEY` → en-tête
  `X-Admin-Key` sur `/v1/admin/*` ; **403** sinon. L'ancienne route
  `DELETE /v1/usage/{key}` (réinitialisation anonyme) a été supprimée.
- Résultats : un `job_id` n'est lisible que par la clé qui a soumis le
  document ; toute autre clé reçoit **404**, indistinguable d'un job
  inconnu (aucune sonde d'existence).
- Quotas par clé : `STATEMENTFLOW_QUOTA_DOCUMENTS` / `_PAGES` → **429**.

### Réponses d'erreur structurées (v0.2.0)

Toute erreur HTTP (hors erreurs de traitement de job) :
```json
{"error": {"code": "unauthorized", "message": "Clé d'API invalide.",
           "request_id": "6f1c…"}}
```
Chaque réponse porte l'en-tête `X-Request-ID` (reprise de l'en-tête client
s'il existe). Codes stables : `invalid_file` (400) · `unauthorized` (401) ·
`forbidden` (403) · `not_found` (404) · `file_too_large` (413) ·
`processing_error` (422, portée par le job) · `quota_exceeded` (429).

### Configuration (variables d'environnement, préfixe `STATEMENTFLOW_`)

`MAX_FILE_MB` (20) · `MAX_PAGES` (200) · `MAX_ROWS` (200000, plafond
appliqué pendant la lecture — protection ZIP bomb) · `OCR_DPI` (300) ·
`TOLERANCE` (0.01) · `API_HOST` · `API_PORT` · `RESULTS_TTL_S` (900) ·
`USAGE_STORE_PATH` · `API_KEY_HASHES` · `ADMIN_KEY` ·
`QUOTA_DOCUMENTS` · `QUOTA_PAGES`.

## Facturation prépayée (v0.3.0)

Le service géré se paie en crédits : **1 crédit = 1 page PDF ou 50 lignes
CSV/TSV/XLSX** (minimum 1 par document, décompté après un traitement
réussi — un document rejeté ne consomme rien). Packs et prix :
docs/PRICING.md (5 000 / 20 000 / 60 000 FCFA).

Parcours d'achat :
1. `GET /v1/billing/packs` — catalogue public ;
2. `POST /v1/billing/topup` `{"pack": "pro", "customer_email": "…",
   "customer_name": "…"}` → `{reference, checkout_url}` (paiement mobile
   money/carte via SasPay) ;
3. le client paie sur `checkout_url` ;
4. les crédits sont activés **uniquement** après confirmation serveur :
   webhook `transaction.success` signé (HMAC-SHA256 sur
   `{timestamp}.{corps brut}`, tolérance 300 s, idempotence par
   événement) **ET** re-vérification du paiement auprès du prestataire ;
5. `GET /v1/billing/status/{reference}` — statut (pending/activated/
   failed) ; le polling rattrape tout webhook manqué.

Une clé sans crédits suffisants reçoit **402** sur `/v1/analyze`
(`billing_mode: "managed"`). Les clés déclarées via
`STATEMENTFLOW_API_KEY_HASHES` restent non facturées (`unmanaged`).
`GET /v1/usage` renvoie `credits_remaining` et `billing_mode`.

Encaissement : SasPay (docs.saspay.me). Production : configurer
`STATEMENTFLOW_SASPAY_SECRET_KEY` (clé sk_live), un secret webhook
(`STATEMENTFLOW_SASPAY_WEBHOOK_SECRET`, configuré dans le dashboard
SasPay) et `STATEMENTFLOW_BILLING_STORE_PATH`. Sans clé SasPay, le mode
test simule la passerelle localement (mêmes contrats, aucun paiement
réel) : voir `statementflow billing simulate`.

En mode test uniquement (aucune clé SasPay réelle ET
`STATEMENTFLOW_TEST_PAYMENT=1`), la route `POST /v1/billing/_test/pay`
simule un paiement et renvoie le webhook signé (corps en base64 pour un
transport byte-exact) ; avec une clé SasPay réelle elle répond toujours
404 — aucune simulation possible en production.

Gestion des clés par l'opérateur (voir CLI) :
```
statementflow keys issue <label> --credits 100   # clé client + crédits
statementflow keys list                          # hashes + soldes
statementflow keys credits <clé|hash> 500        # ajuster
statementflow keys revoke <clé|hash>             # révoquer
statementflow billing pending|check|simulate     # rechargements
```
Variables : `STATEMENTFLOW_KEYS_PATH` (fichier JSON des clés gérées,
rechargé à chaud), `STATEMENTFLOW_BILLING_STORE_PATH`.

## CLI

```
statementflow parse <fichier|dossier> [-o out.json] [--outdir dir] [--pretty]
                     [--quiet] [--currency-hint XOF]
statementflow check <resultat.json>     # code retour 2 si déséquilibré
statementflow keys create [--count N]   # clé aléatoire + hash (rotation documentée)
statementflow keys hash [clé]           # empreinte SHA-256 (stdin si omise)
statementflow keys issue <label> [--credits N]  # clé client gérée (KEYS_PATH)
statementflow keys list|revoke|credits <clé|hash>       # gestion
statementflow billing pending|check <ref>|simulate <session>  # rechargements
```

## Application web et comptes clients (v0.5.0)

Le produit s'utilise désormais AUSSI depuis un navigateur :
`https://statementflow-api-ch0f.onrender.com/` (landing, démo publique,
inscription, tableau de bord). Détails : docs/FRONTEND.md.

Nouvelles routes (résumé) :

| Méthode | Route | Auth | Usage |
|---|---|---|---|
| POST | `/v1/auth/signup` | — | Créer un compte (email+mot de passe, crédits d'accueil, clé API visible une fois) |
| POST | `/v1/auth/login` / `logout` | — / cookie | Connexion / déconnexion (cookie HttpOnly `sf_session`) |
| GET | `/v1/auth/session` | cookie | État de session (email, crédits, clé masquée) |
| POST | `/v1/auth/verify` / `verify/resend` | token | Vérification d'email (token usage unique) |
| POST | `/v1/auth/password/forgot` / `reset` | token | Réinitialisation (1 h, usage unique) |
| GET | `/v1/account/summary` / `conversions` / `topups` | cookie | Tableau de bord (solde, historiques) |
| POST | `/v1/account/convert` (multipart) | cookie | Conversion — MÊME moteur et MÊME facturation que /v1/analyze |
| GET | `/v1/account/result/{job_id}` | cookie | Résultat (isolé par compte) |
| POST | `/v1/account/topup` `{pack}` | cookie | Achat d'un pack (montant calculé SERVEUR) |
| GET | `/v1/account/topup/{reference}` | cookie | Statut + activation par polling (idempotent avec le webhook) |
| POST | `/v1/account/key/rotate` | cookie | Régénérer la clé API (solde transféré) |
| POST | `/v1/demo/analyze` (multipart) | — | Démo publique (2 Mo, 6/h/IP, tronquée) |
| POST | `/v1/track` | — | Visites (attribution ?ref=/referrer) |
| GET | `/v1/admin/stats` | X-Admin-Key | Statistiques commerciales (CA = UNIQUEMENT les paiements activés) |

Exemple minimal Python (compte + conversion) :

```python
import requests

API = "https://statementflow-api-ch0f.onrender.com"
s = requests.Session()  # conserve le cookie de session

s.post(f"{API}/v1/auth/signup",
       json={"email": "client@example.com", "password": "motdepasse123"})

with open("releve.pdf", "rb") as f:
    job = s.post(f"{API}/v1/account/convert",
                 files={"file": ("releve.pdf", f)}).json()
print(job["job_id"], s.get(f"{API}/v1/auth/session").json())
```

## Webhook