#!/usr/bin/env python3
"""Simulation financière StatementFlow — 3 scénarios × 5 tailles.

Hypothèses (toutes explicites, sources vérifiées 2026-11-08) :
- Prix des packs (décision, voir docs/PRICING.md) : Starter 500 crédits /
  5 000 FCFA ; Pro 2 500 / 20 000 ; Business 10 000 / 60 000 →
  10 / 8 / 6 FCFA par crédit (dégressif).
- Frais d'encaissement SasPay (saspay.me, consulté 2026-11-08) :
  1,75 % + 100 FCFA fixe par transaction. Paiements uniques (packs),
  pas d'abonnement → pas de frais récurrents chez le prestataire.
- Coût variable de traitement : mesure locale v0.2.0 — PDF natif
  ~0,09 s/14 pages, CSV 100k lignes 4,1 s. Hypothèse serveur mutualisé
  (type VPS ~5 000-10 000 FCFA/mois, payé par les premiers revenus) :
  coût CPU marginal ~0,05 FCFA/crédit ; OCR plus lourd → 0,10 FCFA/page
  scannée. Nous prenons 0,10 FCFA/crédit (marge de sécurité).
- Infrastructure au lancement : 0 FCFA (pilote local / machine de
  l'opérateur) ; à partir de 50 clients : 10 000 FCFA/mois (VPS + domaine
  + monitoring), financée par le chiffre d'affaires.
- Consommation moyenne par client (hypothèse centrale, à valider en
  pilote) : prudent 250 crédits/mois, réaliste 600, ambitieux 1 200.
- Répartition des packs : prudent 60/35/5 % (S/P/B), réaliste 40/45/15,
  ambitieux 25/50/25.

Sorties : CA, frais de paiement, coût variable, infrastructure,
bénéfice avant impôts, marge, et seuil de rentabilité mensuel.
"""
import json

PACKS = {'S': (500, 5000), 'P': (2500, 20000), 'B': (10000, 60000)}
SASPAY_PCT = 0.0175
SASPAY_FIX = 100.0          # FCFA par transaction
VAR_COST_PER_CREDIT = 0.10  # FCFA (CPU/OCR, marge de sécurité)
INFRA = {0: 0.0, 50: 10000.0}  # FCFA/mois : 0 client → local ; ≥50 → VPS


def infra_for(n_clients: int) -> float:
    for threshold in sorted(INFRA, reverse=True):
        if n_clients >= threshold:
            return INFRA[threshold]
    return INFRA[0]


def simulate(n_clients: int, credits_per_month: float,
             mix: dict) -> dict:
    """Un mois d'activité pour n clients actifs payants."""
    total_credits = n_clients * credits_per_month
    # répartition des achats : les crédits sont achetés par packs
    revenue = fees = 0.0
    credits_bought = 0.0
    for pack, share in mix.items():
        credits_pack, price = PACKS[pack]
        # nombre de packs achetés (arrondi) pour couvrir la consommation
        n_packs = round(total_credits * share / credits_pack) or (
            1 if n_clients and share else 0)
        revenue += n_packs * price
        fees += n_packs * (price * SASPAY_PCT + SASPAY_FIX)
        credits_bought += n_packs * credits_pack
    var_cost = total_credits * VAR_COST_PER_CREDIT
    infra = infra_for(n_clients)
    profit = revenue - fees - var_cost - infra
    return {
        'clients': n_clients,
        'credits_consommes': round(total_credits),
        'credits_achetes': round(credits_bought),
        'ca_fcfa': round(revenue),
        'frais_paiement_fcfa': round(fees),
        'cout_variable_fcfa': round(var_cost),
        'infrastructure_fcfa': round(infra),
        'benefice_avant_impots_fcfa': round(profit),
        'marge_brute_pct': round(100 * (revenue - fees - var_cost)
                                 / revenue, 1) if revenue else None,
    }


SCENARIOS = {
    'prudent': {'credits': 250, 'mix': {'S': 0.60, 'P': 0.35, 'B': 0.05}},
    'realiste': {'credits': 600, 'mix': {'S': 0.40, 'P': 0.45, 'B': 0.15}},
    'ambitieux': {'credits': 1200, 'mix': {'S': 0.25, 'P': 0.50, 'B': 0.25}},
}
SIZES = [10, 50, 100, 500, 1000]

results = {name: {n: simulate(n, s['credits'], s['mix'])
                  for n in SIZES} for name, s in SCENARIOS.items()}

# seuil de rentabilité mensuel (scénario réaliste, mix Pro dominant) :
# trouver le revenu qui couvre frais + variable + infra (10 000 FCFA)
# marge nette par FCFA de CA ≈ 1 - (0,0175 + fixe moyen/panier) - 0,10*r
# calcul direct par itération sur le nombre de clients
print(f'{"scénario":10s} {"clients":>8s} {"CA":>10s} {"frais pgmt":>10s} '
      f'{"coût var":>9s} {"infra":>7s} {"bénéfice":>10s} {"marge %":>7s}')
for name, runs in results.items():
    for n, r in runs.items():
        print(f'{name:10s} {n:8d} {r["ca_fcfa"]:10,d} '
              f'{r["frais_paiement_fcfa"]:10,d} {r["cout_variable_fcfa"]:9,d} '
              f'{r["infrastructure_fcfa"]:7,d} '
              f'{r["benefice_avant_impots_fcfa"]:10,d} '
              f'{r["marge_brute_pct"] or 0:7.1f}')
    print()

# seuil : plus petit nombre de clients (réaliste) à bénéfice > 0
seuil = next((n for n in SIZES
              if results['realiste'][n]['benefice_avant_impots_fcfa'] > 0), None)
print(f'Seuil de rentabilité (scénario réaliste, infra 10 000 FCFA dès '
      f'50 clients) : {seuil} clients actifs payants' if seuil
      else 'Rentabilité non atteinte dans les tailles simulées.')

# détail d'un panier : frais SasPay sur chaque pack
print('\nFrais SasPay par pack (1,75 % + 100 FCFA) :')
for pack, (credits, price) in PACKS.items():
    fee = price * SASPAY_PCT + SASPAY_FIX
    print(f'  {pack:5s} {credits:6d} crédits  {price:7,d} FCFA → '
          f'frais {fee:8,.0f} FCFA  net {price - fee:8,.0f} FCFA '
          f'({100 * fee / price:.1f} %)')

with open('/home/z/my-project/scripts/finance_simulation.json', 'w',
          encoding='utf-8') as f:
    json.dump({'hypotheses': {
        'frais_saspay': '1,75 % + 100 FCFA (saspay.me, 2026-11-08)',
        'cout_variable_fcfa_par_credit': VAR_COST_PER_CREDIT,
        'infrastructure': {'0_client': 0, '50_clients': 10000},
        'consommation_mensuelle_par_client': {
            k: v['credits'] for k, v in SCENARIOS.items()}},
        'resultats': results,
        'seuil_rentabilite_clients': seuil}, f, ensure_ascii=False, indent=2)
print('\n→ scripts/finance_simulation.json')
