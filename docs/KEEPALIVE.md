# Anti-sommeil Render — fonctionnement et limites

## Objectif

Le plan Render **gratuit** met un service en veille après une période
sans requête. La première requête suivante paie alors un « réveil » de
~20-60 secondes. Le workflow
[`.github/workflows/render-keepalive.yml`](../.github/workflows/render-keepalive.yml)
interroge `/health` toutes les 10 minutes pour réduire ces périodes
d'inactivité.

## Fonctionnement

- **Déclenchement** : cron GitHub Actions `*/10 * * * *` (UTC), ou
  déclenchement manuel (bouton *Run workflow* / `workflow_dispatch`) ;
- **Sonde** : `curl` GET `https://statementflow-api-ch0f.onrender.com/health`
  avec `--max-time 90` (le réveil d'un service endormi peut prendre
  jusqu'à ~60 s) et jusqu'à **3 tentatives** espacées de 20 s ;
- **Validation** : la réponse JSON doit avoir `status == "ok"` ET
  `persistence == "postgres+up"` — sinon le workflow **échoue** avec un
  message explicite (visible dans l'onglet Actions et, si configuré,
  par notification d'échec de workflow) ;
- **Anti-parallélisme** : `concurrency` avec `cancel-in-progress` —
  deux exécutions ne se chevauchent jamais ;
- **Sécurité** : aucune donnée modifiée (GET en lecture seule sur un
  endpoint public), aucun secret utilisé, aucun secret dans les sorties
  (la réponse /health n'en contient aucun).

## Limites — à lire avant de compter dessus

1. **Ce n'est PAS une garantie de disponibilité permanente.** Entre
   deux sondes (jusqu'à 10 minutes), le service peut être mis en
   veille ; GitHub Actions peut retarder un cron sur un runner chargé ;
   Render peut appliquer sa propre politique de mise en veille ou
   redémarrer le service lors des déploiements.
2. **Consommation de minutes Actions.** ~4 464 exécutions/mois × ~30 s
   ≈ 37 heures de minutes. Illimité pour un **dépôt public** ; à
   surveiller pour un dépôt privé (2 000-3 000 minutes gratuites/mois
   selon le plan) — dans ce cas, espacer le cron (`*/30` ou horaire).
3. **Coût réel de la fiabilité.** Si la disponibilité permanente
   devient indispensable (clients en production), la solution fiable
   est une instance Render payante (à partir de ~7 $/mois), qui n'est
   jamais mise en veille. Aucune instance payante n'a été créée sans
   autorisation explicite.

## Vérifier que le keepalive fonctionne

- Onglet **Actions** du dépôt → workflow « Keepalive Render » : les
  exécutions doivent être vertes et dater de moins de 10 minutes ;
- Un échec = API injoignable ou PostgreSQL down → le message d'erreur
  indique lequel (`status` ou `persistence`).

## Désactivation

Supprimer le fichier `.github/workflows/render-keepalive.yml` (ou
commenter la section `schedule:`) — aucune trace côté Render, le
service redevient simplement sujet à la mise en veille standard.
