# Conditions Générales d'Utilisation — StatementFlow (service géré)

Version du 2026-11-08 — modèle à faire valider juridiquement avant toute
commercialisation effective (voir §8). Le code source est sous licence
MIT (usage libre) ; les présentes conditions concernent le SERVICE GÉRÉ
(API opérée par l'exploitant), distinct du logiciel.

## 1. Objet

StatementFlow convertit des documents financiers fournis par le client
(relevés bancaires, exports mobile money aux formats PDF/CSV/TSV/XLSX)
en données structurées JSON avec contrôles de cohérence comptable. Le
service ne fournit **aucun accès aux comptes** bancaires ou mobile money,
ne sollicite aucun identifiant, mot de passe ou code OTP, et n'émet
aucun avis financier, comptable ou juridique.

## 2. Accès et clés

L'accès à l'API requiert une clé (en-tête `X-API-Key`). La clé est
personnelle au client, qui en assure la confidentialité et notifie
immédiatement toute compromission (révocation immédiate possible). Des
quotas et limites techniques s'appliquent (taille, pages, lignes —
publiées sur `/health`). L'usage doit rester conforme à la loi ; le
client s'interdit toute tentative de contournement des limites ou
d'accès aux données d'autrui.

## 3. Crédits prépayés et encaissement

Le service se paie par crédits prépayés (1 crédit = 1 page PDF ou 50
lignes tabulaires ; minimum 1 par document ; décompte uniquement sur
traitement réussi). Les packs et prix sont publiés sur `/v1/billing/packs`
et dans docs/PRICING.md. Le paiement est traité par un prestataire
(SasPay) ; les crédits sont activés après confirmation serveur du
paiement. Les crédits sont valables 12 mois après achat, sans
reconduction tacite ; ils ne sont ni remboursables ni cessibles, sauf
défaillance prouvée du service. Des crédits d'essai gratuits peuvent
être accordés à titre promotionnel, révocables.

## 4. Documents fournis — engagements du client

Le client déclare être **autorisisé** à transmettre les documents traités
(documents propres, ou de tiers dans le cadre d'un mandat). Il reste
unique responsable de la licéité du traitement et du respect des lois
applicables (données personnelles, secret bancaire). Le service est un
outil de traitement : le client est responsable de traitement au sens
réglementaire, l'exploitant agit comme simple outil/sous-traitant
technique.

## 5. Garde des données et confidentialité

Traitement éphémère : les documents sont traités en mémoire et jamais
écrits sur disque par le moteur ; les résultats sont conservés au
maximum 15 minutes (TTL) puis purgés ; un redémarrage efface tout.
Aucune donnée bancaire n'est journalisée. Politique complète :
docs/PRIVACY.md. Suppression sur demande : contact (§8).

## 6. Garanties et limites — honnêteté contractuelle

Le service est fourni « en l'état ». Les performances de parsing
publiées proviennent d'un corpus synthétique de test ; **aucune garantie
de taux de réussite n'est donnée sur des documents réels**. Le service
signale ses incertitudes (alertes, statuts « non vérifiable ») mais la
vérification finale des données reste à la charge du client. Le service
n'est pas certifié conforme à quelque réglementation que ce soit
(RGPD, CDP Sénégal). La responsabilité totale de l'exploitant, toutes
causes confondues, est plafonnée au montant des crédits achetés par le
client au cours des 12 derniers mois.

## 7. Disponibilité et support

Aucun engagement de disponibilité (SLA) n'est offert à ce stade (phase
pilote). Support gratuit via les GitHub Discussions/Issues du dépôt
public. Interruptions de maintenance possibles sans préavis.

## 8. Contact, droit applicable et réserves

Contact : via le dépôt GitHub public (issues) — canal officiel du
pilote. Droit sénégalais (UEMOA) pour un exploitant basé au Sénégal,
sous réserve de validation juridique humaine. **Document de travail non
validé juridiquement** : avant toute vente effective, faire relire ces
CGU par un professionnel du droit et adapter le droit applicable, les
mentions d'identification (SIREN/NINEA, siège) et les obligations
locales de facturation.

---
*Modèle de travail édité le 2026-11-08 dans le cadre de la préparation
commerciale de StatementFlow v0.3.0. Statut : PRÊT POUR PILOTE, PAS POUR
VENTE EFFECTIVE.*
