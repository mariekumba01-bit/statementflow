# StatementFlow

[English](README.md) · [Français](README.fr.md)

[![CI](https://github.com/mariekumba01-bit/statementflow/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mariekumba01-bit/statementflow/actions/workflows/ci.yml) [![Licence : MIT](https://img.shields.io/badge/Licence-MIT-yellow.svg)](https://github.com/mariekumba01-bit/statementflow/blob/main/LICENSE)

**Transformez des relevés bancaires et des exports mobile money en transactions JSON normalisées et vérifiées.**

PDF natifs, PDF scannés (OCR), CSV, TSV, XLSX → dates ISO, libellés nettoyés, débits/crédits séparés, devises détectées quand elles sont identifiables, soldes d'ouverture/clôture, **contrôles de cohérence comptable** et alertes sur les données incertaines. Pensé pour les formats **français, européens et UEMOA / Afrique francophone** (montants FCFA, exports Wave / Orange Money / MTN MoMo / Moov — fournis légalement par leurs titulaires).

- Moteur de règles 100 % local, **aucune clé API, aucun service payant** ;
- CLI, API REST, serveur MCP (agents IA) et SDK Python ;
- **Aucun stockage des documents** : traitement éphémère en mémoire.

> ⚠️ **Honnêteté sur les performances** : les métriques publiées (corpus de test synthétique de 32 documents / 447 transactions) sont des **résultats de tests internes**, pas une garantie sur vos relevés réels. Testez sur vos propres documents avant tout usage de production — c'est exactement ce que l'option `--currency-hint`, les contrôles de cohérence et les alertes sont faits pour valider.

## Démarrage rapide

```bash
# installation (dépendances : pdfplumber, openpyxl — tout est gratuit)
pip install -e .

# OCR des PDF scannés (optionnel, recommandé) :
#   apt install tesseract-ocr poppler-utils

# conversion d'un relevé en JSON
statementflow parse releve.pdf --output result.json --pretty

# dossier complet d'exports → un JSON par document
statementflow parse exports/ --outdir resultat/ --quiet

# contrôles comptables seuls
statementflow check result.json
```

```python
from statementflow import parse_file
r = parse_file('releve.pdf')
print(r['totals'], r['checks']['opening_closing_ok'], r['confidence'])
```

## Exemple de sortie (extrait réel, document synthétique)

```json
{
  "schema": "statementflow/v1",
  "document": {"filename": "SYNTH_fr_societegenerale_A.pdf",
               "kind": "pdf", "pages": 1, "method": "text",
               "parser": "generic-columns-v3", "adapter": "generic-pdf"},
  "currency": "EUR",
  "opening_balance": 3812.53,
  "closing_balance": -3319.45,
  "transactions": [
    {"index": 1, "date": "2026-01-01", "label": "CB ABONNEMENT NETFLIX",
     "debit": 859.35, "credit": null, "balance": 2953.18, "currency": "EUR",
     "confidence": 1.0,
     "trace": {"page": 1, "method": "text", "source_row": 7}}
  ],
  "checks": {"chain_checked": 14, "chain_matches": 14, "chain_ok": true,
             "opening_closing_ok": true,
             "totals": {"count": 14, "debits": 7507.2, "credits": 6375.22}},
  "alerts": [],
  "confidence": 1.0
}
```

Exemples complets générés par le moteur : [`outputs/`](outputs/) (relevé FR, export Wave CSV, PDF scanné OCR avec alertes). Documentation du schéma : [`docs/API.md`](docs/API.md).

## Les quatre interfaces

| Interface | Commande / point d'entrée | Usage |
|---|---|---|
| **CLI** | `statementflow parse <fichier|dossier>` | traitement local, scripts shell |
| **API REST** | `uvicorn statementflow.api:app` → `/docs` (OpenAPI) | intégrations HTTP |
| **MCP** | `statementflow-mcp` (stdio) | agents IA (Claude, Cursor…) |
| **SDK Python** | `from statementflow.sdk import Client` | applications Python |

```bash
# API REST (documentation interactive sur http://127.0.0.1:8080/docs)
uvicorn statementflow.api:app --host 127.0.0.1 --port 8080
# toute route privée exige X-API-Key (toute clé non vide en mode ouvert local)
curl -X POST http://127.0.0.1:8080/v1/analyze -H "X-API-Key: ma-cle" -F "file=@releve.pdf"
curl http://127.0.0.1:8080/v1/results/<job_id> -H "X-API-Key: ma-cle"

# serveur MCP (à déclarer dans votre client MCP)
statementflow-mcp
```

Exemples d'intégration prêts à l'emploi : [`examples/`](examples/) — Python, JavaScript/TypeScript, curl, **n8n** (workflow JSON importable), client MCP.

## Ce que fait le moteur

- **Formats d'entrée** : PDF natifs (pdfplumber), PDF scannés (OCR tesseract à 300 dpi avec repli automatique), CSV/TSV/XLSX (détection de séparateur, BOM UTF-8, entête décalée).
- **Montants** : `1 234,56` (FR, avec espace insécable), `1.234,56` (DE/ES), `1,234.56` (UK), `1 234 567 XOF` (FCFA entier), parenthèses comptables `(750,38)`, signes suffixes `1.234,56-`.
- **Dates** : ISO, `31/01/2026`, `31.01.2026`, `31-01-2026`, années 2 chiffres — toujours normalisées en ISO 8601 ; les dates impossibles deviennent `null` + alerte (jamais inventées).
- **Débit/crédit** : colonnes séparées, montants signés, ou sens déduit d'une colonne « Sens/Type » (RECEPTION/ENVOI/Cash-in…) — un montant positif sans aucun signal de direction est marqué **débit supposé + avertissement**.
- **Contrôles comptables** : chaîne des soldes ligne à ligne, règle d'ensemble (ouverture + crédits − débits = clôture), détection de doublons (répétitions de page supprimées, doublons adjacents signalés).
- **Alertes et confiance** : chaque transaction porte une confiance (abaissée en cas de date nulle, montant manquant, OCR faible) et une **traçabilité** (page, méthode, ligne source) ; chaque document porte des alertes codifiées (`date_unparseable`, `currency_not_identified`, `chain_break`, `ocr_used`…).

## Métriques de test (corpus synthétique — voir [`tests/`](tests/))

| Mesure | Valeur | Note |
|---|---|---|
| Rappel / précision transactions | **100 % / 100 %** | 447/447 extraites, 0 inventée |
| Dates | 98,0 % | 9 dates *impossibles* dans le corpus → `null` + alerte |
| Libellés, débits, crédits, soldes | **100 %** | |
| Documents parfaitement convertis | 27/32 | les 5 restants : comportements honnêtes documentés |
| Devise | 67 % | 5 CSV sans aucune devise lisible → `null` + alerte (jamais inventée) |

Mesurées par script (`scripts/mesure_corpus.py`) sur le corpus synthétique
figé ; la v0.2.0 a aussi corrigé un bug de harnais dans la formule de
précision publiée (pondérée par transactions, pas par documents — la valeur
réelle était déjà 1.0).

Suite de tests : **206 tests pytest** (unités, corpus avec seuils figés de non-régression, les 4 correctifs, API, CLI, MCP en JSON-RPC réel, fichiers malformés, comptabilité).

```bash
python -m pytest tests/ -q          # 206 passed
```

## Sécurité et confidentialité

- **Aucun stockage** : les octets sont traités en mémoire puis jetés ; aucun document n'est écrit sur disque par le moteur.
- API : résultats conservés en mémoire avec TTL (15 min par défaut), purge automatique.
- **Authentification API obligatoire** (v0.2.0) : toute route privée exige
  `X-API-Key` (401 sinon) ; résultats liés à la clé soumettrice (404 pour
  toute autre clé) ; clés stockées uniquement en SHA-256 côté serveur,
  comparaison à temps constant ; administration séparée
  (`STATEMENTFLOW_ADMIN_KEY`) ; quotas par clé (429).
- Aucune journalisation de données bancaires (seuls nom de fichier nettoyé,
  taille, codes d'erreur).
- Limites strictes : 20 Mo / 200 pages / 200 000 lignes par défaut (variables d'environnement `STATEMENTFLOW_*`).
- Le produit n'est **pas certifié RGPD** et ne prétend à aucune conformité sans audit juridique indépendant — voir [`docs/SECURITY.md`](docs/SECURITY.md) et [`docs/BILLING.md`](docs/BILLING.md).

## Statut de maturité

**Bêta — aperçu développeur.** Le moteur est complet sur ses formats cibles
et couvert par 206 tests, mais il n'a **été validé que sur le corpus
synthétique** : aucun relevé bancaire réel n'a encore été traité. Considérez
chaque résultat comme non vérifié tant qu'un pilote sur des documents que
vous êtes autorisé à traiter ne l'a confirmé (`docs/PILOT.md`). Aucune
certification de conformité.

## Limites connues

1. Les métriques proviennent d'un **corpus synthétique** ; les relevés réels (mis en page par lots, polices exotiques, en-têtes graphiques) peuvent échouer — les alertes et la confiance sont là pour le signaler.
2. OCR : la confiance est plafonnée (≤ 0,75, 0,6 si faible) ; certains PDF scannés à très faible qualité peuvent donner des montants erronés — **vérifiez toujours les alertes** `chain_break`/`ocr_low_confidence`.
3. La devise n'est jamais devinée : documents sans devise lisible → `currency: null` + alerte (fournissez `--currency-hint`).
4. Formats testés : français, DE/NL/ES/UK, UEMOA (XOF), mobile money (Wave, Orange Money, MTN MoMo, Moov). D'autres banques peuvent nécessiter un adaptateur ([`docs/FORMATS.md`](docs/FORMATS.md)).
5. Relevés chiffrés/mots de passe : rejetés avec une erreur explicite.
6. Le fichier XLS legacy (`.xls` binaire) n'est pas supporté (XLSX uniquement).

## Documentation

- [`docs/API.md`](docs/API.md) — schéma JSON complet, endpoints, codes d'alerte
- [`docs/MCP.md`](docs/MCP.md) — serveur MCP et outils exposés
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — conception, adaptateurs, extension
- [`docs/FORMATS.md`](docs/FORMATS.md) — formats supportés et modèles de relevés
- [`docs/SECURITY.md`](docs/SECURITY.md) — traitement des données, limites
- [`docs/BILLING.md`](docs/BILLING.md) — comptage d'usage, quotas, facturation future (aucune passerelle intégrée)
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — auto-hébergement : prêt / à configurer / à sécuriser
- [`docs/PILOT.md`](docs/PILOT.md) — kit pilote zéro budget : démonstration, fiche d'évaluation, modèles
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) — erreurs courantes et solutions

## Licence

MIT — voir [`LICENSE`](LICENSE).

## Contribution

Voir [`CONTRIBUTING.md`](CONTRIBUTING.md). Règle d'or : toute correction doit passer par un test de non-régression ; le corpus de test n'est jamais modifié pour faire passer les résultats.
