# Dépannage

## Installation

| Symptôme | Cause probable | Solution |
|---|---|---|
| `ModuleNotFoundError: No module named 'statementflow'` | installation éditable perdue (venv recréé) | `pip install -e ".[dev,api,mcp]"` depuis la racine du dépôt |
| `statementflow: command not found` | Scripts du venv hors PATH | activer le venv (`source .venv/bin/activate`) ou `python -m statementflow.cli …` |
| `OCRUnavailable` / « Installez tesseract et poppler-utils » | PDF scanné sans outils OCR | `apt install tesseract-ocr poppler-utils` (Debian/Ubuntu) — inutile pour PDF natifs/CSV/XLSX |
| Tests : `csv.Error: line contains NUL` | Python ≤ 3.10 avec version < 0.2.0 | mettre à jour (correctif inclus depuis v0.2.0) |

## Moteur

| Symptôme | Explication | À faire |
|---|---|---|
| `currency: null` + `currency_not_identified` | aucune devise lisible dans le document — **jamais inventée** | repasser avec `--currency-hint XOF` (CLI) ou en-tête `X-Currency-Hint` (API) |
| `date: null` + `date_unparseable` | date illisible OU impossible (31/02, 29/02 non bissextile) — jamais corrigée en silence | vérifier la ligne source (`trace.source_row`), corriger à la main |
| `side_assumed_debit` | montant positif sans colonne Sens ni signe | vérifier la transaction ; fournir un format avec colonnes Débit/Crédit si récurrent |
| `chain_break` / `unbalanced_statement` | incohérence comptable réelle ou lecture fautive | comparer `expected_balance` vs `actual_balance` ; si le document est sain, signaler le cas (adapter) |
| `ocr_low_confidence` | scan de mauvaise qualité | re-scanner en 300 dpi ; les montants restent à vérifier |
| Fichier rejeté « limite » | > 20 Mo / 200 pages / 200 000 lignes | ajuster `STATEMENTFLOW_MAX_*` ou découper ; la limite protège la mémoire |
| Montants ≥ 1e13 imprécis | lecture flottante au-delà de ~1e13 d'unités | hors plage réaliste ; une alerte de rupture apparaît (comportement sûr) |

## API

| Symptôme | Cause | Solution |
|---|---|---|
| `401 unauthorized` | `X-API-Key` absent, vide ou inconnu | fournir une clé valide ; en production, configurer `STATEMENTFLOW_API_KEY_HASHES` (`statementflow keys create`) |
| `404` sur `/v1/results/{job_id}` | job expiré (TTL 15 min), inconnu, **ou soumis par une autre clé** | relancer l'analyse avec la clé propriétaire |
| `403 forbidden` sur `/v1/admin/*` | en-tête `X-Admin-Key` absent/invalide, ou `STATEMENTFLOW_ADMIN_KEY` non configuré | configurer la clé admin et l'envoyer dans `X-Admin-Key` |
| `429 quota_exceeded` | quota de la clé atteint (`STATEMENTFLOW_QUOTA_*`) | attendre la fenêtre suivante ou réinitialiser via admin |
| `413 file_too_large` | fichier > `MAX_FILE_MB` | réduire ou augmenter la limite |
| `/health` dit `auth_mode: "open"` | aucun hash de clé configuré | mode développement local ; pour la production, configurer les hashes |

## MCP

| Symptôme | Cause | Solution |
|---|---|---|
| Serveur ne démarre pas | module absent dans l'environnement du client MCP | pointer la config MCP sur le venv qui a StatementFlow installé |
| `No module named 'mcp.server.fastmcp'` | SDK mcp 2.x sans StatementFlow ≥ 0.2.0 | mettre à jour (shim v1/v2 inclus depuis 0.2.0) |
| Outils absents du client | transport stdio mal configuré | vérifier `command`/`args` dans la config client (voir `docs/MCP.md`) |

## Diagnostic général

```bash
statementflow --version                     # version installée
statementflow parse <doc> --pretty          # sortie complète + alertes
statementflow check <resultat.json>         # contrôles seuls (code retour 2 = déséquilibré)
python -m pytest tests/ -q                  # 206 tests attendus
```
