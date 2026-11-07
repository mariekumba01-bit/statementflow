# Architecture StatementFlow

```
                    ┌────────────────────────────────────────────┐
  fichier (octets)  │              engine.parse_bytes            │
  ─────────────────►│                                            │
                    │  1. detect_kind (signature + extension)    │
                    │  2. detect_adapter (config par format)     │
                    │  3. extraction                             │
                    │     ├─ PDF  : pdf_backend (texte | OCR)    │
                    │     └─ CSV/XLSX : tabular (sniffing)       │
                    │  4. normalisation (core : montants, dates, │
                    │     devises, directions)                   │
                    │  5. checks (chaîne, ensemble, doublons)    │
                    │  6. alertes + confiance + traçabilité      │
                    └───────────────────┬────────────────────────┘
                                        │  dict statementflow/v1
        ┌───────────────┬───────────────┼────────────────┬─────────────┐
        ▼               ▼               ▼                ▼             ▼
      CLI             API REST      serveur MCP       SDK Python   tests
   (cli.py)    (api.py/FastAPI)  (mcp_server.py)      (sdk.py)   (pytest)
```

## Modules

| Module | Rôle |
|---|---|
| `core.py` | Primitives pures : montants multiformats, dates, devises, rôles de colonnes, modèles (`Transaction`, `Alert`). Aucune E/S. |
| `pdf_backend.py` | Lignes et tokens d'un PDF : couche texte pdfplumber, repli OCR (pdftoppm + tesseract, 300 dpi), fusion des fragments de milliers. |
| `tabular.py` | Lecture CSV/TSV/XLSX : détection de séparateur, BOM, entête dans les 10 premières lignes, synonymes multilingues. |
| `engine.py` | Orchestration : détection de colonnes par clustering des bords droits, assignation clusters↔rôles, assemble le résultat. |
| `checks.py` | Contrôles comptables (chaîne des soldes, règle d'ensemble), déduplication, confiance document. |
| `adapters.py` | Point d'extension : configurations par format (voir ci-dessous). |
| `config.py` | Limites et options (variables d'environnement `STATEMENTFLOW_*`). |
| `usage.py` | Compteurs d'usage et quotas (préparation facturation, aucune passerelle). |
| `api.py`, `cli.py`, `mcp_server.py`, `sdk.py` | Interfaces. |

## Décisions de conception

**Pourquoi des règles et pas un LLM ?** Déterminabilité (même entrée → même sortie), coût marginal nul (~5×10⁻⁶ $/relevé), fonctionnement hors ligne, aucune donnée envoyée à un tiers. Le benchmark interne (voir `tests/expected_metrics.json` et le dossier de validation) montre aussi que la précision est supérieure ou égale sur les formats cibles.

**Clustering des bords droits** : les montants sont alignés à droite dans quasi tous les relevés ; on clusterise les `x1` de tous les tokens numériques du document (tolérance 12 pt) et on apparie les clusters aux rôles numériques de l'entête (ordre gauche→droite ; si des clusters parasites subsistent — fragments OCR —, on garde les plus peuplés). Plus robuste que la moyenne des centres car tolère les colonnes à faible densité.

**Honnêteté par défaut** : plutôt que d'inventer, le moteur émet `null` + alerte (devise, date) ou marque une hypothèse + avertissement (sens débit supposé). La confiance est dégressive (date nulle −0,25 ; montant manquant −0,25 ; OCR ≤ 0,75/0,6 ; rupture de chaîne → 0,5) et plafonnée à 0,5 au niveau document en cas d'erreur.

## Ajouter un format bancaire

Le moteur est générique ; un format récalcitrant se traite par **adaptateur** (voir `adapters.py`) :

1. Écrire un `Adapter` avec une `detect()` (règle courte sur l'entête/le texte) ;
2. L'enregistrer dans `DEFAULT_ADAPTERS` ;
3. Ne jamais forcer une devise dans `currency_hint` sauf si le format la garantit toujours ;
4. Ajouter un document au corpus de test + un seuil dans `expected_metrics.json`.

Les surcharges utiles : synonymes d'entête additionnels, format de date imposé, indice de devise garanti. Si un format résiste au moteur générique, la cause est presque toujours (a) une entête non reconnue → synonymes, (b) des colonnes non alignées à droite → adapter dédié minimal.

## Tests et non-régression

- `tests/test_engine_corpus.py` rejoue le corpus complet et vérifie les seuils **figés** de `expected_metrics.json` — toute régression fait échouer la CI ;
- `tests/test_fixes.py` couvre les 4 correctifs historiques (entêtes « (suite) », dates impossibles, OCR fragmenté, devise absente) ;
- `tests/test_malformed.py` garantit qu'aucune entrée hostile ne crashe le moteur ;
- le corpus (`tests/data/corpus/`, 32 documents synthétiques étiquetés `SYNTH_`) n'est **jamais modifié** pour faire passer les résultats.
