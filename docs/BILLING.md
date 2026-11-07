# Comptage d'usage et facturation future

**État actuel : aucune monétisation n'est opérationnelle.** Aucune passerelle de paiement n'est intégrée, aucun prestataire n'est configuré, aucune facture ne peut être émise. Ce document décrit l'infrastructure de comptage (réelle, testée) qui permettra de brancher un prestataire plus tard **sans réécrire le moteur**.

## Ce qui existe (code + tests)

- **Compteurs par clé d'API** (`usage.py`) : documents, pages, transactions — en mémoire ou persistés en JSON (`STATEMENTFLOW_USAGE_STORE_PATH`), thread-safe, réinitialisables (`DELETE /v1/usage/{key}`).
- **Chaque résultat embarque son usage** : `"usage": {"pages": 3, "transactions": 214}` — la mesure est faite à la source, pas reconstruite.
- **Quotas extensibles** (`Quota`) : plafonds par documents/pages vérifiés à chaque requête (`429` si dépassé) ; enregistrables par clé. Fenêtre mensuelle, plans multiples : à implémenter au moment voulu (extension prévue par design).

## Ce qui n'existe pas (et n'est pas simulé)

- Passerelle de paiement (Stripe, Polar, etc.) ;
- gestion des abonnements ;
- facturation et TVA ;
- authentification forte des clés (l'`X-API-Key` actuelle est un identifiant de comptage, pas un mécanisme de sécurité).

## Chemin vers la facturation à l'usage (quand un compte prestataire existera)

1. Créer le compte prestataire (action **humaine** : inscription, KYC, validation bancaire) ;
2. Exposer `GET /v1/usage` aux clients finaux (déjà fait) ;
3. Brancher les webhooks du prestataire sur les compteurs persistés ;
4. Activer les quotas par plan ;
5. Ajouter l'authentification (clés signées) avant toute exposition publique.

Le moteur, la CLI et le mode local **resteront gratuits et fonctionnels sans aucun de ces éléments**.
