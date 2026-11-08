# Politique de confidentialité — StatementFlow (service géré)

Version du 2026-11-08. Ce document décrit le traitement réel, vérifiable
dans le code (dépôt public, licence MIT). Il n'est pas un avis juridique
et ne vaut pas certification ; les validations réglementaires humaines
restent à faire (§5).

## 1. Données traitées

- **Documents fournis par le client** (relevés bancaires, exports mobile
  money PDF/CSV/TSV/XLSX) : contiennent des données financières
  personnelles (nom, numéro de compte, transactions). Traités UNIQUEMENT
  en mémoire, jamais écrits sur disque par le moteur ; les fichiers
  temporaires de l'OCR vivent dans un répertoire temporaire de l'OS et
  sont supprimés à la fin du traitement.
- **Résultats d'analyse** (JSON) : conservés en mémoire du processus au
  maximum 15 minutes (TTL configurable), accessibles uniquement avec la
  clé ayant soumis le document, purgés à l'expiration ; un redémarrage du
  service les efface intégralement.
- **Compteurs d'usage** : agrégats (nombre de documents/pages/
  transactions, crédits) associés au HASH SHA-256 de la clé d'API —
  jamais à la clé en clair, jamais à une identité.
- **Données de facturation** : email et nom fournis lors de l'achat
  d'un pack, transmis au prestataire de paiement (SasPay) pour
  l'émission du paiement ; l'exploitant conserve la référence
  d'achat, le pack et les montants (obligations comptables).

## 2. Finalités

Fournir le service souscrit : convertir les documents en JSON, compter
les crédits consommés, encaisser les recharges. **Aucune autre
finalité** : pas de profilage, pas de publicité, pas d'entraînement de
modèle sur les documents, pas de revente, pas de statistiques au-delà
des compteurs agrégés.

## 3. Durées de conservation

| Donnée | Durée |
|---|---|
| Documents | 0 (jamais écrits ; mémoire vive du traitement uniquement) |
| Résultats | ≤ 15 min (TTL) |
| Compteurs d'usage | durée de vie de la clé ; purge à la révocation ou sur demande |
| Historique d'achats | durée des obligations comptables locales (à confirmer juridiquement — hypothèse : 10 ans) |

## 4. Destinataires et sous-traitants

- **Aucun tiers** ne reçoit les documents ni les résultats.
- Le prestataire de paiement (SasPay, saspay.me) reçoit les données
  strictement nécessaires à l'encaissement (email, nom, montant).
- Hébergement : au stade pilote, machine contrôlée par l'exploitant
  (bind 127.0.0.1 par défaut) ; en production, un hébergeur sera
  contractuellement encadré (DPA) — à établir avant la mise en ligne.

## 5. Base légale et droits

Traitement sur la base de l'exécution du service demandé par le client
(consentement au transfert de ses documents). Selon les juridictions
applicables (Sénégal — loi 2008-12 et CDP ; UE — RGPD), les personnes
concernées disposent de droits (accès, rectification, effacement…)
exerçables auprès du client responsable de traitement ; l'exploitant,
en qualité d'outil/sous-traitant, apporte son concours (suppression
immédiate : les données ne sont de toute façon pas persistées).
**Validation juridique humaine requise** avant tout traitement de
documents de tiers : déclaration/registre, statut de l'exploitant,
transferts éventuels.

## 6. Sécurité

Clés d'API stockées uniquement sous forme de hash SHA-256 ; comparaisons
à temps constant ; administration séparée ; limites de taille et de
pages ; validation des formats par signature binaire ; aucun secret dans
les journaux ; erreurs structurées sans détail interne. Détails
techniques et registre : docs/SECURITY.md.

## 7. Suppression

Sur demande (issue GitHub ou canal de contact du pilote) : révocation de
la clé, purge des compteurs, suppression de l'historique d'achat dans la
limite des obligations comptables. Les documents et résultats, non
persistés, ne nécessitent aucune action. Procédure opérateur :
docs/PILOT.md §7.

---
*Modèle technique de travail (2026-11-08), à valider juridiquement avant
commercialisation effective.*
