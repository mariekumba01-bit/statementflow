"""Exemple SDK Python — usage local (moteur embarqué, aucun serveur requis).

    python examples/exemple_python.py
"""
from statementflow.sdk import Client

c = Client()
r = c.parse('tests/data/corpus/SYNTH_fr_societegenerale_A.pdf')

print(f"{r.document['filename']} : {len(r.transactions)} transactions")
print(f"  devise={r.currency} ouverture={r.opening_balance} "
      f"clôture={r.closing_balance}")
print(f"  équilibré : {r.is_balanced} | confiance : {r.confidence}")
for t in r.transactions[:3]:
    sens = 'débit' if t['debit'] else 'crédit'
    montant = t['debit'] or t['credit']
    print(f"  {t['date']} {t['label'][:38]:<40} {sens:>7} {montant:>10} "
          f"(confiance {t['confidence']})")

# export CSV prêt pour un tableur / logiciel comptable
r.to_csv('/tmp/statementflow_export.csv')
r.to_json('/tmp/statementflow_export.json')
print('\nExports écrits : /tmp/statementflow_export.csv et .json')

# exemple avec indice de devise (export mobile money sans devise lisible)
r2 = c.parse('tests/data/corpus/SYNTH_csv_orange_money_V.csv',
             currency_hint='XOF')
print(f"Orange Money : {len(r2.transactions)} tx, devise={r2.currency}")
