# Sécurité et confidentialité

## Traitement des données (état réel, sans certification)

- **Éphémère** : le moteur traite les octets en mémoire et ne les écrit jamais sur disque. Les fichiers temporaires de l'OCR sont créés dans un répertoire temporaire OS et supprimés (contexte Python garanti).
- **Rétention API** : les résultats (JSON) sont conservés en mémoire du processus pendant au maximum `RESULTS_TTL_S` (15 min par défaut), puis purgés ; un redémarrage du service les efface intégralement.
- **Journalisation** : aucun contenu bancaire n'est journalisé — seuls le nom de fichier (nettoyé : chemin et caractères de contrôle retirés), la taille et les codes d'erreur traversent les messages d'erreur. Les clés d'API ne sont jamais journalisées ni renvoyées (réponses masquées `mask_key`).
- **Réseau** : mode local par défaut (bind 127.0.0.1). Aucune télémétrie, aucun appel sortant, aucun envoi de document à un tiers. CORS désactivé par défaut (même origine) — CORS n'est pas un mécanisme d'authentification.

## Ce que le produit ne fait JAMAIS

- Aucune demande de mot de passe bancaire, de code OTP, d'identifiant ou d'accès à un compte.
- Aucune connexion à une banque ou à un opérateur mobile money : le produit ne traite que **les documents que l'utilisateur lui fournit** et que cet utilisateur est autorisé à transmettre (documents propres, ou de ses clients avec mandat).
- Aucun contournement d'authentification, aucun scraping de service financier.

## Authentification et clés d'API (v0.2.0)

- Toute route privée exige `X-API-Key` (401 si absente/invalide) ; les
  résultats sont liés à la clé soumettrice (404 indistinguable pour une
  autre clé) ; l'administration (`/v1/admin/*`) exige une clé distincte
  (`STATEMENTFLOW_ADMIN_KEY`, 403 sinon).
- **Stockage hashé** : le serveur ne connaît que des empreintes SHA-256
  (`STATEMENTFLOW_API_KEY_HASHES`) ; la clé admin est hashée au chargement.
  Comparaisons en temps constant (`hmac.compare_digest`). La clé en clair
  n'est jamais stockée, journalisée ni renvoyée.
- **Cycle de vie des clés** : `statementflow keys create` (génération
  256 bits + empreinte), `statementflow keys hash` (empreinte d'une clé
  existante, lecture stdin recommandée pour éviter l'historique shell).
  **Rotation** : ajouter le nouveau hash à `API_KEY_HASHES`, redémarrer,
  distribuer la nouvelle clé, puis retirer l'ancien hash pour révoquer.
- **Mode ouvert** (sans hashes configurés) : toute clé non vide est
  acceptée — réservé au développement local, signalé par
  `/health → auth_mode: "open"`. Production : configurez les hashes.
- Les compteurs d'usage sont indexés par hash de clé ; la
  réinitialisation est une opération administrative uniquement
  (l'ancienne route anonyme `DELETE /v1/usage/{key}` a été supprimée).

## Protections techniques

- Validation de type par signature binaire (`%PDF-`, `PK\x03\x04`) et extension — les fichiers mensongers sont rejetés avec une erreur explicite.
- Limites par défaut : 20 Mo, 200 pages, 200 000 lignes (surchargeables via `STATEMENTFLOW_*`) — le plafond de lignes est appliqué **pendant** la lecture (CSV comme XLSX), ce qui borne la mémoire face à un ZIP bomb ou un CSV géant.
- Les PDF corrompus ou chiffrés produisent une erreur propre (jamais de crash). Les macros ne sont jamais exécutées (XLSX lu en `read_only`, `data_only` — formules non évaluées).
- Noms de fichiers fournis par les clients nettoyés (`sanitize_filename` : path traversal, caractères de contrôle, longueur) — utilisés uniquement comme métadonnées, jamais pour accéder au système de fichiers.
- Identifiants de job non dévinables (UUID4) ; identifiants de requête `X-Request-ID` sur toutes les réponses ; erreurs structurées sans détail interne ni secret.
- Traitement borné dans le temps : OCR limité (timeout subprocess), limites de pages/lignes ; un document trop lourd produit une erreur claire plutôt qu'un blocage.
- Dépendances : audit `pip-audit` sans vulnérabilité connue au 2026-11-07 (job CI `security-audit` informationnel).
- Secrets uniquement via variables d'environnement ; le dépôt n'en contient aucun.

## Registre de traitement (niveau technique — à compléter par l'exploitant)

| Élément | État technique |
|---|---|
| Nature des données | relevés bancaires / exports mobile money fournis par l'utilisateur : transactions (dates, libellés, montants, soldes), devise du compte |
| Finalité | conversion en données structurées vérifiées, à la demande de l'utilisateur |
| Base légale | à définir par l'exploitant (intérêt légitime, contrat, consentement) — **validation juridique humaine requise** |
| Durée de conservation | documents : 0 (jamais écrits) ; résultats API : ≤ `RESULTS_TTL_S` (15 min) ; compteurs d'usage : agrégats (pages/transactions) par hash de clé, tant que le magasin est configuré |
| Suppression | automatique (TTL, temporaires OS) ; redémarrage du service = effacement mémoire ; magasin d'usage : suppression du fichier `USAGE_STORE_PATH` |
| Sous-traitance | aucun tiers : traitement local, pas de sous-traitant cloud par défaut |
| Responsabilités | l'exploitant de l'instance est responsable de traitement ; StatementFlow est un outil auto-hébergé (pas un service) |
| Mesures | chiffrement en transit à la charge de l'exploitant (TLS/reverse-proxy) ; pas de données bancaires au repos ; accès par clés hashées ; quotas |

## Cadre juridique — ce qui exige une attention extérieure

StatementFlow traite des **données personnelles financières**. Le déploiement auprès de tiers (cabinets, fintechs) implique :

- RGPD (UE) : le client cabinet est responsable de traitement, l'opérateur du service est sous-traitant (contrat art. 28, DPA) — obligations de minimisation, durée de conservation définie, registre des traitements ;
- Sénégal/UEMOA : loi n° 2008-12 et déclaration préalable auprès de la CDP pour les traitements de données personnelles ;
- confidentialité bancaire selon les juridictions concernées.

**Ce logiciel n'est pas certifié conforme** à quelque réglementation que ce soit, et ne le prétend pas. Avant tout traitement de documents de tiers, obtenez un avis juridique professionnel et les consentements appropriés ; en attendant, limitez le traitement à vos propres documents ou à des documents synthétiques/anonymisés. Ce document est une analyse technique, **pas un avis juridique**.

## Signalement

Vulnérabilité suspectée : ouvrir une issue privée via le dépôt (security advisory) sans y joindre de document bancaire réel.
