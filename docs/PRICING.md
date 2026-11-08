# Tarification StatementFlow (v0.3.0)

## Modèle : prépaiement par crédits

Pas d'abonnement, pas d'engagement. Le client achète des crédits, les
consomme au fil des traitements, recharge quand il veut. Une seule règle
de consommation, publique et vérifiable :

> **1 crédit = 1 page PDF traitée, ou 50 lignes d'un document tabulaire
> (CSV / TSV / XLSX). Minimum 1 crédit par document.**

Les crédits sont décomptés **après un traitement réussi** (un document
rejeté — corrompu, trop volumineux, format inconnu — ne consomme rien).
Les clés de démonstration/pilote peuvent recevoir des crédits offerts par
l'opérateur (`statementflow keys issue --credits N`).

## Packs (FCFA, XOF)

| Pack | Crédits | Prix | Prix/crédit | Usage type |
|---|---|---|---|---|
| Starter | 500 | 5 000 | 10 FCFA | ~10 relevés PDF de 10 pages, ou exports mobile money |
| Pro | 2 500 | 20 000 | 8 FCFA | automatisation n8n mensuelle, petit cabinet |
| Business | 10 000 | 60 000 | 6 FCFA | volume cabinet/fintech, rapprochements récurrents |

Tarifs dégressifs (10 → 8 → 6 FCFA/crédit), cohérents avec la
concurrence : les APIs de parsing de relevés facturent entre ~0,004 et
0,29 USD par page (comparatif Holofin de 14 outils, 2026 ; Mindee
~0,06 USD/page sur plan Starter 53 USD/mois, Veryfi 0,25 USD/relevé avec
minimum 500 USD/mois). À 10 FCFA/crédit (~0,016 USD/page), StatementFlow
se place **sous les APIs comparables** tout en incluant les formats
mobile money (Wave, Orange Money, MTN MoMo, Moov) qu'aucun outil du
comparatif ne couvre.

## Ce qui est inclus dans chaque crédit

- PDF natif ou scanné (OCR), CSV, TSV, XLSX, français/anglais + formats
  UEMOA et mobile money ;
- sortie JSON versionnée (`statementflow/v1`) : transactions, dates
  validées calendriquement, montants, soldes, statut de validation ;
- contrôles comptables : chaîne des soldes, équilibre ouverture/clôture,
  doublons, devise, chronologie ;
- traçabilité (page/ligne source) et alertes codifiées ;
- API, CLI, SDK Python, serveur MCP — même moteur, mêmes crédits ;
- aucun stockage des documents (traitement éphémère, résultats TTL).

## Ce qui n'est PAS vendu

- aucun accès bancaire direct (pas d'agrégateur, pas de connexion Wave/
  Orange Money : l'utilisateur fournit ses documents) ;
- aucune garantie de performance sur des relevés non testés — les
  métriques publiées proviennent d'un corpus synthétique ;
- aucune certification de conformité (RGPD, CDP Sénégal) ;
- pas de conseil financier ni d'analyse de données.

## Frais et marges (informatif, sources au 2026-11-08)

Encaissement SasPay : 1,75 % + 100 FCFA par transaction (pas
d'abonnement) → 3,8 % du pack Starter, 1,9 % du pack Business. Coût
variable de traitement ≈ 0,10 FCFA/crédit (mesures locales v0.2.0,
marge de sécurité incluse). Marge brute ≈ 95-97 %. Seuil de rentabilité :
10 clients actifs payants avec une infrastructure locale (0 FCFA).
Simulation complète : `scripts/finance_simulation.py` (exécutable) et
`docs/BUSINESS_CASE.md`.

## Auto-hébergement / déploiement privé

Le code est publié sous licence MIT (voir docs/BUSINESS_CASE.md §
licence) : un client peut l'auto-héberger gratuitement. Ce que l'offre
payante vend alors est le service géré : clé API immédiate, mises à jour,
nouveaux formats ajoutés au moteur, support. Pour un déploiement privé
avec support (SLA léger, mises à jour, adaptation de formats), tarif
indicatif pilote : pack Business mensuel + forfait d'intégration à
définir au cas par cas (aucun engagement standard à ce stade).

## Encaissement

Paiements uniques par packs via SasPay (mobile money et cartes,
Sénégal/UEMOA). Les crédits sont activés **uniquement** après
confirmation serveur du paiement (webhook signé HMAC + vérification du
statut réel) — jamais sur une redirection navigateur. Mode test complet
disponible sans compte marchand (simulateur local, mêmes contrats API).

## Plan pilote (gratuit, avant première vente)

`statementflow keys issue pilote-<nom> --credits 100` : 100 crédits
offerts (≈ 100 pages) pour évaluer le produit sur des documents
autorisés, avec la fiche de retour de docs/PILOT.md. Critères de
validation : ≥ 3 pilotes ayant consommé leurs crédits gratuits ET
demandé une recharge payante.
