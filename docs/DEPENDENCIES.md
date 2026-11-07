# Dépendances et licences

StatementFlow est distribué sous licence MIT (voir LICENSE).

## Dépendances d'exécution (toutes gratuites et open source)

| Paquet | Licence | Usage |
|---|---|---|
| pdfplumber (pdfminer.six, pypdfium2) | MIT | extraction de texte/positions des PDF natifs |
| openpyxl | MIT | lecture XLSX |

## Dépendances optionnelles

| Paquet | Licence | Usage |
|---|---|---|
| fastapi, uvicorn, starlette, pydantic | MIT/BSD | API REST (`[api]`) |
| mcp (SDK officiel Model Context Protocol) | MIT | serveur MCP (`[mcp]`) |
| pytest, httpx | MIT/BSD | tests (`[dev]`) |

## Outils système optionnels

| Outil | Licence | Usage |
|---|---|---|
| tesseract-ocr | Apache-2.0 | OCR des PDF scannés |
| poppler-utils (pdftoppm) | GPL-2.0 | rastérisation des pages avant OCR |

Note : poppler-utils est un **outil système appelé en sous-processus**, non lié au code du moteur — l'usage reste compatible avec la distribution MIT de StatementFlow. Sans ces outils, les PDF scannés sont rejetés avec une erreur explicite ; tout le reste fonctionne.

## Corpus de test

32 documents **synthétiques** générés par un script déterministe (voir `tests/data/corpus/inventaire.csv`, nature « synthétique » déclarée pour chaque fichier). Aucun relevé réel n'est inclus dans le dépôt.
