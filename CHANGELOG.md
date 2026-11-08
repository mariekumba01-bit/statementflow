# Changelog

## 0.4.1 (2026-11-08) — incident PostgreSQL Render : DSN pollué par un caractère invisible, fuite de secret corrigée

### Cause racine (incident production)
Un `U+200E` (LEFT-TO-RIGHT MARK, catégorie Unicode `Cf`) copié-collé
devant `STATEMENTFLOW_DATABASE_URL` empêchait libpq de reconnaître
l'URI (`missing "=" after "postgresql://…" in connection info
string`). `str.strip()` ne retire PAS ces caractères (`isspace()` est
False) — la valeur Render semblait propre visuellement.

### Corrections
- **`sanitize_dsn`** : le DSN est nettoyé à l'entrée du `PgStore`
  (marques bidi U+200E/U+200F, BOM, zero-width U+200B-U+200D, NBSP,
  contrôles Cc, espaces) — un copier-coller pollué fonctionne tel
  quel, sans retoucher la variable Render.
- **Anti-fuite de secret** : le DSN est pré-validé (parsing libpq)
  AVANT toute connexion — les messages d'erreur de parsing contiennent
  la chaîne COMPLÈTE (mot de passe compris) et étaient journalisés par
  le pool. Désormais : `PgStoreError` actionnable (nom de variable,
  format attendu), jamais le DSN ; `/health` expose `pg_error` (type
  classifié : `dsn_invalid`/`auth_failed`/`tls_error`/`unreachable`)
  et le cron n'imprime plus aucun message d'erreur brut.
- **Pooler Supabase (port 6543, mode transaction)** : les requêtes
  préparées persistantes sont désactivées automatiquement pour ce port
  (requis par pgBouncer) — sinon erreurs intermittentes
  « prepared statement … does not exist ».
- **Coexistence des clés** : avec `STATEMENTFLOW_API_KEY_HASHES` (clés
  techniques) ET PostgreSQL (clés clients gérées), une clé gérée est
  désormais ACCEPTÉE — auparavant la hash-list seule passait, rendant
  les opérations financières (réservation/débit de crédits) impossibles
  en production. `/health` affiche `key-hash-list+managed-keys`.
- **`connect_direct`** : connexion hors pool (verrou du cron)
  pré-validée elle aussi.
- **render.yaml** : service cron COMMENTÉ (prêt à activer) — les Cron
  Jobs Render exigent un plan payant, aucune dépense n'est engagée ;
  test manuel gratuit documenté dans docs/CRON.md.

### Tests
- 15 nouveaux (`tests/test_dsn_diagnostics.py`) : sanitisation
  (U+200E/U+200F/BOM/ZWSP/NBSP/Cc), no-leak du mot de passe sur les
  deux chemins d'échec, classification, e2e avec DSN pollué sur un
  VRAI PostgreSQL (ping + persistance après fermeture/réouverture du
  pool), pooler 6543, coexistence des clés (analyse facturée sur
  PostgreSQL avec hash-list configurée), maintenance sortie 3 sans
  fuite. Total : 289 tests verts (246 base + 28 PG + 15 DSN).

## 0.4.0 (2026-11-08) — persistance PostgreSQL réelle, Cron Job Render, correction du déploiement

### Déploiement (corrections de blocage)
- **`python-multipart>=0.0.9`** ajouté à l'extra `api` : sans lui,
  l'API Render plantait au premier upload (`RuntimeError: Form data
  requires "python-multipart" to be installed`) — cause racine de
  l'indisponibilité du service.
- Nouvel extra **`pg`** (`psycopg[binary,pool]>=3.2`) ; le Dockerfile
  installe `.[api,pg]` (persistance prête sans rebuild).

### Persistance PostgreSQL / Supabase (branchée, testée)
- **Nouveau module `statementflow.pg` (PgStore)** : adaptateur
  PostgreSQL remplaçant les magasins JSON quand
  `STATEMENTFLOW_DATABASE_URL` est défini — MÊMES méthodes publiques
  que `ManagedKeyStore`/`BillingStore` (l'API, la CLI et les tests
  permutent sans changer d'appel). Pool de connexions psycopg3,
  reconnexion après coupure, DSN masqué dans les erreurs (aucun
  secret journalisé).
- L'API utilise PostgreSQL automatiquement : clés gérées, soldes,
  rechargements, usage, métadonnées de jobs. `/health` expose
  `persistence: "postgres+up|down"` (ou `"json"`).
- Les opérations financières passent par les fonctions SQL
  transactionnelles (verrous `for update`, grand livre immuable) ;
  **toute l'opération reserve/settle/refund partage le job_id** →
  idempotence par job réelle.
- Migration **004** (correctifs d'intégrité) : `settle_credits`
  idempotent (rejeu sans violation UNIQUE) et plafonné au solde
  disponible (**jamais de solde négatif**) ; `reserve_credits`
  idempotent par job. Corrections SQL : `in schema public` (revoke),
  pgcrypto tolérant (natif PG13+), rôle `anon` tolérant hors Supabase.
- **Tests : 28 nouveaux** (`tests/test_pg.py`) exécutés contre un
  VRAI PostgreSQL (pgserver local / service conteneurisé en CI) :
  migrations appliquées, RLS deny-all vérifié, cycle complet analyse
  facturée → achat → webhook signé → activation → rejeu sans double
  crédit → isolation entre clés, réconciliation cron, verrou
  consultatif. CI : job `pg-tests` dédié (postgres:16).

### Facturation — conformité à la règle publique
- La consommation tabulaire compte désormais les **lignes réelles**
  du document (`usage.rows`, en-tête inclus) et non le nombre de
  transactions reconnues : « 1 crédit = 1 page PDF ou 50 lignes
  CSV/TSV/XLSX » est maintenant exact (31 lignes → 1 crédit,
  121 → 3). Repli sur `transactions` pour les résultats antérieurs.

### Cron Job Render (nouveau)
- **`statementflow.maintenance`** (module) + service `cron` dans
  `render.yaml` : nettoyage (jobs expirés, webhooks > 30 j),
  réconciliation SasPay (pending > 1 h interrogés réellement,
  activation/échec via les fonctions idempotentes), intégrité
  financière (soldes négatifs, crédits sans paiement, paiements sans
  crédit — signalés en CRITIQUE, jamais corrigés automatiquement),
  surveillance (ping base, journal d'audit, code de sortie 2 sur
  anomalie critique). Verrou `pg_try_advisory_lock` sur connexion
  dédiée contre les exécutions simultanées ; sans
  `DATABASE_URL`, sortie 0 (rien à maintenir). Fréquence horaire
  justifiée dans `docs/CRON.md` (renvois SasPay ~2 h 30).
- ⚠️ Les Cron Jobs Render ne sont pas disponibles sur le plan free
  (minimum 0.5c-512mb) : le service cron du blueprint est optionnel.

### Documentation
- `docs/SUPABASE.md` réécrit (branchez en 10 minutes, migrations
  001→004, vérifications, repli JSON) ; `docs/CRON.md` nouveau ;
  `docs/BILLING.md` aligné sur l'implémentation réelle (il décrivait
  la facturation comme « future » alors qu'elle est livrée) ;
  `docs/DEPLOYMENT.md` (variables, cron, health) ; CHANGELOG.

## 0.3.1 (2026-11-08) — durcissement commercial : verrouillage par défaut, crédits atomiques, Supabase, Render

### Sécurité (rupture de compatibilité volontaire)
- **Verrouillage par défaut** : sans `STATEMENTFLOW_API_KEY_HASHES` ni
  `STATEMENTFLOW_KEYS_PATH`, les routes privées répondent **503**
  `auth_not_configured` (`/health` → `auth_mode: "locked"`,
  `status: "degraded"`). L'API n'est plus JAMAIS accessible sans
  authentification ; le mode ouvert (développement local) s'active
  explicitement via `STATEMENTFLOW_ALLOW_OPEN_AUTH=1`.
- Mode « clés gérées » fermé : avec `KEYS_PATH` mais sans
  `API_KEY_HASHES`, une clé inconnue du magasin géré est refusée
  (401) — l'ajout d'un fichier de clés ne rouvre pas l'API.
- Uploads : la limite de taille est appliquée **pendant la lecture**
  (chunks de 1 Mo) — un fichier volumineux est rejeté (413) sans être
  chargé en mémoire.
- Traitement (OCR/parsing) déplacé en threadpool : la boucle
  d'événements n'est plus bloquée pendant un parsing lourd.

### Crédits — atomicité et correction de course
- **Réservation atomique** (`reserve`/`settle`/`refund`) : 1 crédit est
  réservé avant traitement, complété après succès au coût réel
  (règle publique inchangée), restitué sur échec. La relecture du
  fichier de clés se fait désormais **sous le verrou** : deux
  requêtes concurrentes sur un solde de 1 crédit ne peuvent plus
  passer toutes les deux (course démontrée par test, corrigée).
- Webhook SasPay : un statut serveur `PENDING` ou inconnu lors de la
  re-vérification ne marque plus le rechargement `failed` — seul un
  statut d'échec explicite le fait ; le polling tranche ensuite
  (correction : un client payé pouvait perdre ses crédits sur une
  confirmation différée).
- L'API n'accède plus aux attributs internes des magasins
  (`is_revoked`, `snapshot` publics).

### Persistance Supabase (préparée, non appliquée)
- `supabase/migrations/` : schéma complet (organisations, clés, grand
  livre des crédits, rechargements, événements webhook, usage, jobs,
  audit) avec idempotence par contraintes UNIQUE, fonctions
  transactionnelles (`reserve_credits`, `settle_credits`,
  `activate_topup`, `record_webhook_event`, `record_usage`) et RLS
  deny-all. Procédure et correspondance code ↔ schéma : `docs/SUPABASE.md`.

### Déploiement Render
- `docker/start.sh` : port dynamique `$PORT` (Render) avec repli 8080,
  arrêt gracieux ; `render.yaml` (blueprint, secrets hors dépôt,
  healthcheck `/health`) ; `docs/DEPLOYMENT.md` mis à jour.
- CI : workflow renommé `.github/workflows/tests.yml` (push main +
  PR vers main, badges mis à jour).

### Tests
- +16 tests critiques (fichier `tests/test_hardening.py`) :
  concurrence réserve (8 threads, 1 crédit), remboursement sur échec,
  coût réel post-traitement, webhook PENDING vs FAILED explicite,
  limite de taille streamée, persistance des magasins JSON au
  redéploiement, isolation des résultats entre clés gérées.
- Suite complète : **246 tests passent** (230 avant).

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
