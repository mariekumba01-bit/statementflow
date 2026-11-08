# Business case StatementFlow — décisions et preuves (2026-11-08)

Ce document consigne les décisions commerciales définitives de la v0.3.0,
les preuves qui les fondent, la simulation financière exécutable et les
limites honnêtes. Sources vérifiées le 2026-11-08 (URLs en fin de
document). Aucun client, paiement ou témoignage n'existe à ce jour.

## 1. Ce que StatementFlow vend exactement

Une **API de conversion de relevés financiers en JSON vérifié**, facturée
en crédits prépayés. Le client achète un résultat mesurable : des
transactions structurées (date validée, débit/crédit, solde, devise,
identifiants stables) avec contrôles comptables et alertes — pas une
promesse d'« IA ». Livraison : API REST, CLI, SDK Python, serveur MCP
(agents IA), workflows n8n — un seul moteur, une seule unité de compte.

**Différenciateur vérifié** : aucun des 14 outils du comparatif Holofin
(2026) ne mentionne Wave, Orange Money, MTN MoMo ou aucun format mobile
money ; leur couverture est bancaire (UE/US). StatementFlow gère nativement
les exports mobile money UEMOA + relevés FR/EU — c'est le créneau
d'entrée, la banque européenne étant le volume de repli.

## 2. Segment initial choisi : développeurs et intégrateurs qui
automatisent des traitements de documents financiers Afrique francophone

Cibles : fintechs/développeurs Dakar/Abidjan/Cotonou qui rapprochent des
paiements mobile money, agences construisant des automatisations n8n,
éditeurs d'agents IA (MCP), petits cabinets comptables UEMOA.

Pourquoi ce segment (et pas un autre) :

- **Douleur** : manuelle et répétitive — les exports Wave/OM/MTN arrivent
  en PDF/CSV disparates, sans outil de rapprochement (preuve : guide
  kolonell.com 2026 « Wave + OM accounting reconciliation », threads
  Reddit r/n8n, r/smallbusiness, r/Automate 2023-2026 sur la conversion
  PDF→CSV de relevés) ;
- **Volonté de payé** : le marché paie déjà 0,004-0,29 USD/page pour le
  parsing de relevés bancaires (Holofin 2026 : Mindee 53 USD/mois Starter,
  Veryfi 0,25 USD/relevé + 500 USD/mois minimum, Docparser 39-159 USD/mois,
  Parseur 499 USD/mois) ;
- **Accès sans budget** : développeurs joignables via GitHub, communautés
  n8n (template public), annuaires MCP (registre officiel modelcontextprotocol,
  Glama, mcp.so, Docker Hub — gratuits), forums — pas de publicité payante ;
- **Coût de livraison ≈ 0** : moteur déjà construit (v0.2.0, 246 tests au 0.3.1),
  traitement CPU local, pas d'infrastructure au démarrage ;
- **Fréquence** : rapprochement mensuel récurrent → crédits récurrents.

Segment écarté (pour l'instant) : les banques/lenders US-EU (Ocrolus,
Docsumo : cycles de vente longs, exigences SOC2/ISO, hors budget nul).

## 3. Le problème exact pour lequel ils paient

« Je reçois chaque mois des relevés bancaires et mobile money en
PDF/CSV/Excel que je dois saisir ou rapprocher à la main ; les outils
existants ne comprennent pas les formats mobile money et facturent au
moins 39 USD/mois avec engagement. » StatementFlow transforme ces
documents en JSON fiable en secondes, avec détection des incohérences,
à 10 FCFA la page, sans abonnement.

## 4. Modèle économique retenu : prépaiement par crédits (modèle G+F hybride)

Comparaison des 7 modèles étudiés (résumé ; hypothèses complètes dans la
simulation) :

| Modèle | Verdict | Raison principale |
|---|---|---|
| A. API hébergée à l'usage | **RETENU** (crédits) | aligne coût/valeur, pas d'engagement (frein n°1 des petits clients), encaissement simple de paiements uniques |
| B. Abonnements à quotas | rejeté pour l'instant | SasPay : pas d'abonnement récurrent documenté → prélèvements auto non garantis ; engagement = frein d'acquisition |
| C. À la page/document | rejeté | micro-transactions → frais fixes SasPay (100 FCFA) prohibitifs < 1 000 FCFA |
| D. Licence privée | offre secondaire | viable (MIT le permet, voir §5) mais cycle de vente long ; forfaits au cas par cas |
| E. Intégration/maintenance | opportuniste | marge élevée mais non récurrente ; à vendre sur demande |
| F. Local gratuit + services | déjà le statu quo open source | ne monetize pas l'API gérée |
| G. API + MCP + automatisations | retenu (c'est A) | une seule unité (crédit) sur toutes les interfaces |

Le prépaiement élimine le risque d'impayé, fonctionne avec des liens de
paiement uniques (pas besoin de prélèvement récurrent), et rend l'offre
compréhensible en une phrase.

## 5. Licence et stratégie GitHub (analyse MIT, décision)

Faits juridiques (analyse technique, pas un avis juridique) :
- Le dépôt est publié sous **MIT** : tout tiers peut déjà légalement
  copier, modifier, redistribuer et **commercialiser** le code, sans
  obligation de contribuer en retour (seules la notice de copyright et la
  licence doivent être conservées).
- Rendre le dépôt privé **n'annule rien** : les droits déjà accordés sur
  les versions publiées restent acquis aux détenteurs de copies/forks/
  clones (d9cbe0d et antérieurs). Un fork peut continuer à publier.
- Une version propriétaire « fermée » ne peut pas révoquer les droits
  MIT sur l'existant ; mélanger code MIT + code propriétaire dans un même
  dépôt privé est possible (le MIT est permissif) mais les contributions
  externes acceptées sans CLA resteraient MIT.

**Décision : rester open source MIT (dépôt public), vendre le service
géré.** Justification : la valeur vendue est l'API opérée, la maintenance
et les nouveaux formats — pas le code en lui-même ; l'open source est un
canal d'acquisition développeurs à coût nul (GitHub, annuaires MCP) ; et
la protection par obscurité serait inefficace de toute façon (droits déjà
accordés). Composants séparables plus tard si nécessaire (moteur public /
portail commercial privé) sans changer la licence du moteur.

## 6. Tarification et simulation financière (exécutable)

Grille : Starter 500 crédits / 5 000 FCFA ; Pro 2 500 / 20 000 ;
Business 10 000 / 60 000 (10/8/6 FCFA par crédit). Encaissement SasPay
1,75 % + 100 FCFA/transaction. Coût variable 0,10 FCFA/crédit (mesures
locales v0.2.0 : PDF natif 0,09 s, CSV 100k lignes 4,1 s). Infrastructure :
0 FCFA au lancement (local), 10 000 FCFA/mois dès 50 clients.

Résultats (`python3 scripts/finance_simulation.py`, hypothèses dans le
script) — bénéfice avant impôts par mois :

| Clients | Prudent | Réaliste | Ambitieux |
|---|---|---|---|
| 10 | 92 588 | 101 862 | 125 625 |
| 50 | 158 888 | 259 100 | 480 675 |
| 100 | 268 925 | 488 900 | 912 500 |
| 500 | 1 110 125 | 2 406 100 | 4 602 500 |
| 1 000 | 2 151 850 | 4 881 050 | 9 215 000 |

(FCFA/mois ; consommation 250/600/1 200 crédits/client/mois.)
Marge brute 95-97 %. **Seuil de rentabilité : 10 clients actifs payants**
en infrastructure locale — l'hypothèse la plus incertaine est la
consommation mensuelle moyenne, à valider en pilote.

## 7. Encaissement : SasPay (vérifié, mode test intégré)

Contrats vérifiés sur docs.saspay.me (2026-11-08) : Bearer sk_test/sk_live,
`POST /checkout-sessions/` (montant, devise XOF, email/nom client, URL de
retour), `GET /checkout-sessions/{id}/status/`, `GET /payments/{id}/verify/`
(re-vérifie toujours l'état réel gateway), webhooks `transaction.success`/
`failed` signés HMAC-SHA256 (`X-Webhook-Signature` = hex de
« {timestamp}.{corps brut} », tolérance 300 s, 5 renvois). Frais : 1,75 % +
100 FCFA, pas d'abonnement. KYC requis avant émission de clé (inscription →
KYC → clé API) — **action manuelle du fondateur** : créer le compte
marchand sur app.saspay.me et passer la vérification.

Implémenté (v0.3.0) : `src/statementflow/billing.py` (client + simulateur
de test aux mêmes contrats), routes `/v1/billing/packs|topup|status|
webhook`, idempotence par événement, activation des crédits uniquement
après webhook signé ET re-vérification serveur. **Aucun paiement réel n'a
été testé** (pas de compte marchand) — le mode test couvre le parcours
complet ; la mise en production exige la clé sk_live et le secret webhook
du compte vérifié.

## 8. Plan d'acquisition gratuit (organique, sans contact non sollicité)

1. Publier le template n8n dans la bibliothèque communautaire
   (community nodes / workflows) et le dépôt GitHub ;
2. Soumettre le serveur MCP aux annuaires gratuits : registre officiel
   MCP (modelcontextprotocol.io/registry), Glama, mcp.so, Docker Hub
   (catalogue MCP) — Smithery ferme le 31/10/2026 (source apideck) ;
3. README bilingue + exemple curl 30 secondes + badges CI (déjà) ;
4. Répondre aux threads publics existants (Reddit r/n8n, r/Automate,
   r/smallbusiness — questions déjà identifiées) en apportant l'outil,
   uniquement là où la question est posée ;
5. GitHub Discussions/Issues ouverts pour le support gratuit ;
6. 3-5 pilotes à 100 crédits offerts via réseau direct du fondateur.

Indicateurs suivis : visiteurs GitHub → clones/étoiles → clés pilotes
émises → premiers traitements réussis → consommation des crédits offerts →
demandes de recharge (l'indicateur de validation, pas les inscriptions).

## 9. Preuves manquantes avant validation commerciale

- aucun pilote n'a encore traité ses documents (0 clé émise hors tests) ;
- la consommation réelle (crédits/mois) est une hypothèse ;
- aucun paiement réel SasPay n'a été exécuté (compte marchand absent) ;
- les métriques du moteur restent mesurées sur corpus synthétique : aucun
  relevé réel autorisé n'a été traité ;
- validation juridique humaine non faite (CDP Sénégal / RGPD, voir
  docs/SECURITY.md).

Critères GO/PIVOT/NO-GO mesurables :
- **GO** (passage production) : ≥ 3 pilotes ont épuisé leurs 100 crédits
  gratuits ET ≥ 1 a payé un pack réel (paiement SasPay confirmé ET
  consommé) ;
- **PIVOT** (segment/format) : ≥ 5 pilotes actifs mais 0 recharge
  demandée sous 60 jours → réinterroger la grille ou le segment ;
- **NO-GO** : impossible d'obtenir 3 pilotes actifs en 90 jours malgré
  la distribution organique → arrêt des frais (ils sont nuls) et
  maintenance open source.

## 10. Risques

- **Techniques** : parsing de vrais relevés non validé (layouts inconnus,
  scans tordus) — atténué par les alertes/statuts « non vérifiable » et
  le pilote ; OCR non corrigé en rotation.
- **Commercial** : concurrence à bas prix (Mistral OCR 4 USD/1k pages)
  mais sans schéma de relevés ni contrôles ; risque d'auto-hébergement
  gratuit (MIT) — assumé, le service géré reste la valeur.
- **Juridiques** : données financières personnelles → déclaration CDP
  Sénégal (loi 2008-12) et RGPD si clients UE, à valider par un humain
  avant tout traitement de documents de tiers ; SasPay : fonds sécurisés
  chez un prestataire non bancaire — limites de responsabilité à lire.
- **Dépendance** : API SasPay jeune (docs évolutives, webhooks
  settlement.* « en cours de fiabilisation » selon leur doc) — le
  simulateur et le polling de statut indépendant du webhook réduisent
  l'exposition.

## Sources (consultées et vérifiées le 2026-11-08)

- holofin.ai/compare/bank-statement-extraction-software — comparatif 14
  outils, prix publics, absence de support mobile money ;
- mindee.com/pricing, docparser.com, parseur.com — grilles publiques ;
- saspay.me (tarifs affichés : 1,75 % + 100 FCFA, pas d'abonnement),
  docs.saspay.me (introduction, checkout-create, checkout-status, verify,
  webhooks — contrats API complets) ;
- kolonell.com (guide rapprochement Wave/OM, juin 2026), joonapay.com ;
- threads Reddit r/n8n, r/Automate, r/smallbusiness (2023-2026) —
  demande réelle de conversion de relevés ;
- apideck.com/blog/mcp-server-directories — annuaires MCP (Smithery en
  fermeture 31/10/2026).
