# Changelog

## 0.1.0 (2026-11-07)

Première version fonctionnelle complète.

### Moteur (moteur générique v3, dérivé du moteur v2 validé)
- PDF natifs (pdfplumber), PDF scannés (OCR tesseract 300 dpi avec repli
  automatique), CSV/TSV/XLSX (détection de séparateur, BOM, entête décalée).
- Montants FR/DE/ES/UK/XOF, parenthèses comptables, signes suffixes,
  espace insécable de milliers ; dates six formats → ISO 8601.
- Quatre correctifs par rapport au moteur v2 : entêtes « (suite) » ignorées ;
  dates impossibles conservées (date=null + alerte) au lieu d'être perdues ;
  OCR fragmenté réparé (fusion 8 pt + clustering top-k) ; devise jamais
  inventée (null + alerte, currency_hint optionnel).
- Contrôles comptables : chaîne des soldes, règle d'ensemble, doublons
  (répétitions de page supprimées, doublons adjacents signalés).
- Alertes codifiées, confiance par transaction et par document, traçabilité
  (page, méthode, ligne source).

### Interfaces
- CLI `statementflow parse|check` (fichier ou dossier).
- API REST FastAPI (health, analyze asynchrone, results TTL, usage,
  OpenAPI ; limites 20 Mo / 200 pages).
- Serveur MCP stdio (parse_statement, get_checks, health).
- SDK Python (mode local et distant).

### Qualité
- 120 tests pytest + batteries manuelles (JSON-RPC MCP réel, TestClient API).
- Corpus synthétique figé (32 documents, 447 transactions) avec seuils de
  non-régression dans tests/expected_metrics.json.
- Métriques internes : rappel/précision 100 %, débits/crédits/soldes/
  libellés 100 %, dates 98 %, devise 67 % (comportements honnêtes documentés).

### Distribution
- Packaging pyproject (PyPI prêt, publication manuelle non effectuée).
- Documentation complète (API, MCP, ARCHITECTURE, FORMATS, SECURITY, BILLING,
  DEPENDENCIES), exemples Python/JS/curl/n8n/MCP, licence MIT.
