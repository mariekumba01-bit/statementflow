# Déploiement — auto-hébergement de l'API StatementFlow

**État actuel : rien n'est déployé publiquement, et rien ne doit l'être sans les
sécurités listées ci-dessous.** Aucun abonnement, aucune dépense engagée. Ce
document distingue explicitement ce qui est prêt, ce qui nécessite une
configuration, et ce qui doit être sécurisé avant toute exposition publique.

## Ce qui est prêt (testé localement)

- **L'API elle-même** : `uvicorn statementflow.api:app` fonctionne, endpoints
  health / analyze / results / usage, OpenAPI sur `/docs`, erreurs propres
  (400/404/413/422/429), TTL des résultats, limites de taille.
- **L'image Docker** : `Dockerfile` fourni (python:3.12-slim + tesseract +
  poppler, utilisateur non-root, healthcheck). Convient à un usage **local ou
  réseau privé** :
  ```bash
  docker build -t statementflow .
  docker run --rm -p 127.0.0.1:8080:8080 statementflow   # local seulement
  ```
- **Le traitement éphémère** : aucun document n'est écrit sur disque ; les
  résultats vivent en mémoire TTL 15 min.
- **Configuration par environnement** (`STATEMENTFLOW_*`) : limites, TTL, port,
  hôte, chemin des compteurs d'usage.

## Ce qui nécessite une configuration (selon l'hébergeur choisi)

| Élément | Statut | Ce qu'il reste à faire |
|---|---|---|
| Hébergement gratuit | non créé | choisir un fournisseur (ex. plan gratuit d'un PaaS conteneur) et créer le compte — action humaine, aucun engagement pris ici |
| Persistance des compteurs | optionnelle | monter un volume/secret sur `STATEMENTFLOW_USAGE_STORE_PATH` |
| HTTPS / TLS | à configurer | terminaison TLS fournie par l'hébergeur ou reverse-proxy ; jamais de HTTP en clair pour des relevés |
| Domaine | non acheté | aucun domaine n'a été acheté ; l'URL d'un plan gratuit suffit pour un pilote |
| CI/CD | prêt | GitHub Actions (`.github/workflows/ci.yml`) teste chaque push/PR ; l'étape de déploiement continu n'est volontairement PAS configurée |

## Ce qui DOIT être sécurisé avant toute exposition publique

1. **Authentification réelle.** L'en-tête `X-API-Key` actuelle n'est qu'un
   identifiant de comptage, pas un mécanisme de sécurité. Avant exposition :
   clés signées (HMAC ou jetons à rotation), gestion des secrets côté
   hébergeur, limitation de débit par clé.
2. **Limite de débit / anti-abus** (429 global, pas seulement par quota).
3. **Taille des requêtes et durée** : vérifier les limites de l'hébergeur
   (body multipart) et garder `MAX_FILE_MB` cohérent.
4. **Base légale du traitement** : les relevés sont des données personnelles
   financières. Avant de traiter des documents de tiers : consentement écrit,
   rôle responsable/sous-traitant (RGPD art. 28, DPA), déclaration CDP
   (Sénégal/UEMOA) le cas échéant, durées de conservation contractualisées.
   **Sans cela, limiter strictement l'API à des documents synthétiques ou
   anonymisés.** Voir `docs/SECURITY.md`.
5. **Audit de sécurité indépendant** avant tout trafic réel.

## Recommandation pour le pilote commercial

Ne pas déployer d'API publique. Le pilote fonctionne en local : le client
transmet un petit lot de documents par canal convenu, le traitement se fait
sur la machine du fondateur (CLI ou API locale), le résultat est renvoyé.
Cette architecture zéro-hébergement est gratuite, conforme au budget, et
n'expose aucun endpoint tant que les sécurités ci-dessus ne sont pas en
place.
