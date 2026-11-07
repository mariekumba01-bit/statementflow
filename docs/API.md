# API StatementFlow — référence

## Schéma JSON de sortie (statementflow/v1)

```jsonc
{
  "schema": "statementflow/v1",
  "document": {
    "filename": "releve.pdf",
    "kind": "pdf | csv | tsv | xlsx",
    "pages": 1,
    "method": "text | ocr | mixed | tabular",
    "parser": "generic-columns-v3",
    "adapter": "generic-pdf | wave-csv | ..."
  },
  "currency": "EUR | XOF | GBP | USD | null",   // null si non identifiable
  "opening_balance": 3812.53,                    // null si absent
  "closing_balance": -3319.45,
  "closing_derived": true,   // false = lu dans le document, true = déduit de la dernière ligne
  "transactions": [
    {
      "index": 1,
      "date": "2026-01-01",          // ISO 8601, null si non interprétable
      "label": "CARTE X4921 AUCHAN",
      "debit": 82.40,                 // jamais négatif ; null si crédit
      "credit": null,                 // jamais négatif ; null si débit
      "balance": 2953.18,             // solde après opération, null si absent
      "currency": "EUR",              // devise de la transaction (colonne) ou du document
      "confidence": 1.0,              // 0.1–1.0, abaissée par les anomalies
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
    "opening_closing_ok": true, // ouverture + crédits − débits = clôture ; null si indis
    "totals": {"count": 14, "debits": 7507.2, "credits": 6375.22}
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
| `date_unparseable` | error | date non interprétable → transaction conservée avec `date: null` |
| `undated_amount_line` | warning | ligne avec montants mais sans date |
| `amount_missing` | warning | ligne datée sans montant reconnu |
| `side_assumed_debit` | warning | montant positif sans signal de direction → débit supposé |
| `possible_duplicate` | warning | doublon adjacent conservé (cas légitime possible) |
| `duplicate_page_repeat` | info | répétition de page supprimée (conservée une fois) |
| `chain_break` | error | solde incohérent ligne à ligne |
| `unbalanced_statement` | error | ouverture + crédits − débits ≠ clôture |

## Endpoints REST

| Méthode | Chemin | Description |
|---|---|---|
| GET | `/health` | état, version, limites |
| POST | `/v1/analyze` | soumet un document (multipart `file`) → `job_id` |
| GET | `/v1/results/{job_id}` | résultat (202 processing / 200 done / 422 erreur / 404 expiré) |
| GET | `/v1/usage` | compteurs de la clé `X-API-Key` |
| DELETE | `/v1/usage/{key}` | remet à zéro les compteurs |
| GET | `/openapi.json`, `/docs` | spécification et documentation interactive |

En-têtes : `X-API-Key` (compteurs d'usage, optionnel), `X-Currency-Hint` (devise si absente du document).

Codes d'erreur : `400` fichier vide · `413` taille dépassée · `429` quota atteint · `422` document invalide (détail dans `error`) · `404` résultat inconnu ou expiré (TTL 15 min).

Configuration par variables d'environnement (préfixe `STATEMENTFLOW_`) : `MAX_FILE_MB` (20), `MAX_PAGES` (200), `MAX_ROWS` (200000), `OCR_DPI` (300), `TOLERANCE` (0.01), `API_HOST`, `API_PORT`, `RESULTS_TTL_S` (900), `USAGE_STORE_PATH` (compteurs persistants).

## CLI

```
statementflow parse <fichier|dossier> [-o out.json] [--outdir dir] [--pretty]
                     [--quiet] [--currency-hint XOF]
statementflow check <resultat.json>     # code retour 2 si déséquilibré
```
