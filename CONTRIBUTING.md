# Contribuer à StatementFlow

## Prérequis

```bash
git clone <votre-fork> && cd statementflow
pip install -e ".[dev,api,mcp]"
# OCR optionnel : apt install tesseract-ocr poppler-utils
python -m pytest tests/ -q        # doit passer : 120 tests
```

## Règles du projet

1. **Le corpus de test est figé.** `tests/data/corpus/` (32 documents synthétiques) et `tests/expected_metrics.json` ne sont jamais modifiés pour faire passer un changement. Si votre amélioration change les métriques, elles ne peuvent qu'augmenter — sinon c'est une régression.
2. **Toute correction de bug passe par un test de non-régression** qui échoue avant le correctif et passe après (voir `tests/test_fixes.py` pour les quatre correctifs historiques).
3. **N'inventez jamais de donnée.** Devise inconnue → `null` + alerte ; date impossible → `null` + alerte ; sens incertain → hypothèse marquée + avertissement. C'est un principe produit, pas une préférence de style.
4. **Aucune dépendance payante**, aucun appel réseau dans le moteur, aucune donnée journalisée.

## Ajouter un format bancaire

Voir [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) § « Ajouter un format ». En résumé : adaptateur déclaratif dans `adapters.py`, document synthétique ajouté au corpus avec sa vérité terrain, seuils mis à jour, test de non-régression.

## Nouvelles sources de documents

Les documents de test sont **synthétiques** (générateur déterministe, préfixe `SYNTH_`, nature déclarée dans `inventaire.csv`). N'ajoutez jamais de relevé réel, même « anonymisé à la main », sans avis juridique préalable. Pour reproduire un bug d'un relevé réel : recréez la structure (colonnes, séparateurs, formats de montants) en synthétique et joignez le modèle.

## Qualité

- Formatage : `python -m compileall src` ; imports triés ; docstrings en français ou anglais, précises ;
- Tests : `python -m pytest tests/ -q` doit rester vert ; les tests manuels (`tests/manual_*_test.py`) démontrent les interfaces réelles (JSON-RPC, TestClient) ;
- Un document réel ne doit jamais figurer dans une issue ou un commit.

## Signaler un bug d'analyse

Ouvrez une issue avec :
1. Un **modèle synthétique** reproduisant la structure du document (générateur ou PDF recréé) ;
2. La sortie du moteur (`statementflow parse model.pdf --pretty`) ;
3. Ce qui était attendu (dates, montants, sens) ;
4. Les alertes émises — souvent, le moteur signale déjà lui-même ce qu'il n'a pas su déterminer.
