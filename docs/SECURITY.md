# Sécurité et confidentialité

## Traitement des données (état réel, sans certification)

- **Éphémère** : le moteur traite les octets en mémoire et ne les écrit jamais sur disque. Les fichiers temporaires de l'OCR sont créés dans un répertoire temporaire OS et supprimés (contexte Python garanti).
- **Rétention API** : les résultats (JSON) sont conservés en mémoire du processus pendant au maximum `RESULTS_TTL_S` (15 min par défaut), puis purgés ; un redémarrage du service les efface intégralement.
- **Journalisation** : aucun contenu bancaire n'est journalisé — seuls le nom de fichier, la taille et les codes d'erreur traversent les messages d'erreur.
- **Réseau** : mode local par défaut (bind 127.0.0.1). Aucune télémétrie, aucun appel sortant, aucun envoi de document à un tiers.

## Ce que le produit ne fait JAMAIS

- Aucune demande de mot de passe bancaire, de code OTP, d'identifiant ou d'accès à un compte.
- Aucune connexion à une banque ou à un opérateur mobile money : le produit ne traite que **les documents que l'utilisateur lui fournit** et que cet utilisateur est autorisé à transmettre (documents propres, ou de ses clients avec mandat).
- Aucun contournement d'authentification, aucun scraping de service financier.

## Protections techniques

- Validation de type par signature binaire (`%PDF-`, `PK\x03\x04`) et extension — les fichiers mensongers sont rejetés avec une erreur explicite.
- Limites par défaut : 20 Mo, 200 pages, 200 000 lignes (surchargeables via `STATEMENTFLOW_*`) — protection contre les fichiers malveillants et les dénis de service accidentels.
- Les PDF corrompus ou chiffrés produisent une erreur propre (jamais de crash).
- Secrets éventuels (clé API d'un futur service hébergé) uniquement via variables d'environnement ; le dépôt n'en contient aucun.

## Cadre juridique — ce qui Exige une attention extérieure

StatementFlow traite des **données personnelles financières**. Le déploiement auprès de tiers (cabinets, fintechs) implique :

- RGPD (UE) : le client cabinet est responsable de traitement, l'opérateur du service est sous-traitant (contrat art. 28, DPA) — obligations de minimisation, durée de conservation définie, registre des traitements ;
- Sénégal/UEMOA : loi n° 2008-12 et déclaration préalable auprès de la CDP pour les traitements de données personnelles ;
- confidentialité bancaire selon les juridictions concernées.

**Ce logiciel n'est pas certifié conforme** à quelque réglementation que ce soit, et ne le prétend pas. Avant tout traitement de documents de tiers, obtenez un avis juridique professionnel et les consentements appropriés ; en attendant, limitez le traitement à vos propres documents ou à des documents synthétiques/anonymisés.

## Signalement

Vulnérabilité suspectée : ouvrir une issue privée via le dépôt (security advisory) sans y joindre de document bancaire réel.
