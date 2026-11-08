# Changelog

## 0.3.0 (2026-11-08) — offre commerciale : crédits prépayés, encaissement SasPay

### Facturation prépayée (nouveau)
- Crédits : 1 crédit = 1 page PDF ou 50 lignes tabulaires, décomptés
  après un traitement réussi (`billing.credits_charged/remaining` dans
  chaque résultat) ; clé sans crédits → 402 `payment_required`.
- Packs : Starter 500/5 000 FCFA, Pro 2 500/20 000, Business 10 000/
  60 000 (10/8/6 FCFA par crédit, dégressif) — catalogue public
  `GET /v1/billing/packs`.
- Achat : `POST /v1/billing/topup` → session de paiement SasPay
  (checkout hébergé) ; statut temps réel `GET /v1/billing/status/{ref}`
  (interroge toujours l'état réel passerelle).
- Webhook `POST /v1/billing/webhook` : signature HMAC-SHA256
  (X-Webhook-Signature sur « {timestamp}.{corps brut} », tolérance
  300 s), idempotence par événement, activation UNIQUEMENT après
  re-vérification serveur du paiement — jamais sur une redirection.
- Encaissement SasPay (docs.saspay.me, contrats vérifiés 2026-11-08) ;
  sans clé configurée, un simulateur local reproduit les mêmes contrats
  (mode test : parcours complet sans paiement réel).
- Clés gérées : `statementflow keys issue|list|revoke|credits`
  (STATEMENTFLOW_KEYS_PATH, hash SHA-256 persistant, rechargement à
  chaud) ; `statementflow billing pending|check|simulate`.

### Documentation commerciale
- docs/PRICING.md (grille, règle de crédits, marges, pilote gratuit),
  docs/BUSINESS_CASE.md (marché, 7 modèles comparés, simulation
  financière exécutable `scripts/finance_simulation.py` : seuil de
  rentabilité à 10 clients actifs, marge brute 95-97 %), docs/TERMS.md
  et docs/PRIVACY.md (modèles CGU/confidentialité, réserve juridique
  explicite), section facturation dans docs/API.md.

### Compatibilité
- Aucun changement cassant : clés `API_KEY_HASHES` restent valides et
  non facturées ; API/CLI/MCP/SDK inchangés fonctionnellement. La
  tarification ne s'applique qu'aux clés gérées (KEYS_PATH).

## 0.2.0 (2026-11-07) — durcissement sécurité, validation calendaire, schéma enrichi

### Sécurité (ruptures volontaires, migration documentée)
- **Authentification obligatoire** : toute route privée exige `X-API-Key`
  (401 si absente/invalide) — l'ancien comportement « clé optionnelle »
  disparaît ; `STATEMENTFLOW_API_KEY_HASHES` configure les clés valides
  (hashes SHA-256 uniquement, comparaison temps constant) ; sans hashes,
  mode ouvert de développement signalé dans `/health`.
- **Isolation des résultats** : un `job_id` n'est lisible que par la clé
  qui a soumis le document (404 indistinguable sinon).
- **Administration séparée** : `/v1/admin/usage` et
  `/v1/admin/usage/reset` exigent `STATEMENTFLOW_ADMIN_KEY` (403 sinon) ;
  la route anonyme `DELETE /v1/usage/{key}` est supprimée.
- Compteurs d'usage indexés par hash de clé (plus de clé en clair au
  repos) ; clés masquées dans les réponses ; `statementflow keys
  create|hash` pour la rotation.
- Quotas par clé réellement appliqués (`STATEMENTFLOW_QUOTA_*`, 429) —
  auparavant déclarés mais jamais câblés.
- Erreurs structurées `{"error": {code, message, request_id}}` +
  `X-Request-ID` sur chaque réponse ; noms de fichiers nettoyés (path
  traversal, caractères de contrôle) ; CORS désactivé par défaut.

### Moteur
- **Validation calendaire stricte des dates** : 31/02, 30/02, 31/04,
  31/06, 29/02 non bissextile, années hors 1900–2100 → `date: null` +
  alerte, jamais converties en une autre date.
- **Contrôles en Decimal** : chaîne des soldes, règle d'ensemble et
  totaux exacts et déterministes (plus de dérive d'accumulation float).
- Nouveaux contrôles : chronologie des dates (`date_order_ok`),
  comptage des lignes sans date / sans montant, devise d'une ligne
  contredisant le document (`currency_mismatch`, alias FCFA→XOF
  normalisés), débit+credit simultanés signalés (PDF).
- **Statut de validation en 4 niveaux** : `validated` / `warning` /
  `inconsistent` / `unverifiable` — l'absence de données n'est plus
  assimilable à une incohérence.
- Plafond de lignes appliqué **pendant** la lecture CSV/XLSX (protection
  ZIP bomb / CSV géant).
- Correctif de traçabilité : les alertes de parsing émises avant la
  déduplication sont remappées sur les index finaux (les alertes
  `side_assumed_debit` des lignes ≥ 2 pointaient vers l'index 1).

### Schéma (rétrocompatible, v0.1 non cassé)
- `document.document_id` (SHA-256 tronqué du contenu), `document_id` +
  `schema_version` + `statement_period` + `account_currency` ;
- `transaction.transaction_id` (stable au rejeu), `description` /
  `original_description` (libellé source conservé), `amount` signé,
  `alerts` par transaction ;
- `metadata` (règles déterministes : interprétation des dates, règle de
  confiance) ; `validation` (statut global).

### Qualité
- 206 tests pytest (was 120) : + matrice d'authentification complète,
  isolation par clé, administration, quotas, request IDs, sanitization,
  dates calendaires (33 cas), schéma v0.2, précision Decimal, plafond
  XLSX ; fixture dépréciée pytest corrigée (0 warning).
- **Bug de harnais corrigé** : la formule de précision du corpus divisait
  par le nombre de documents au lieu du nombre de transactions
  (précision annoncée gonflée). Valeur réelle inchangée : 1.0.
- Métriques corpus re-mesurées par script : recall 1.0, precision 1.0,
  dates 0.9799, devise 0.6742, 27/32 documents parfaits.
- SDK : `usage()` en mode local lève une erreur claire ; erreur 401
  explicite côté distant ; CSV enrichi (transaction_id, description,
  amount).
- Compatibilité SDK `mcp` v1 **et** v2 (shim d'import).
- Documentation : `docs/PILOT.md` (démonstration pilote zéro budget),
  `docs/TROUBLESHOOTING.md`, API.md/SECURITY.md refondues (auth,
  registre de traitement), MCP.md (config client + compat).

### Ruptures (migration)
1. `X-API-Key` requis sur `/v1/analyze`, `/v1/results`, `/v1/usage` —
   prévoir une clé (toute clé non vide en mode ouvert) ;
2. `DELETE /v1/usage/{key}` → remplacé par `POST /v1/admin/usage/reset`
   + `X-Admin-Key` ;
3. `/v1/usage` renvoie `key_masked`/`key_hash` au lieu de la clé en clair.

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
