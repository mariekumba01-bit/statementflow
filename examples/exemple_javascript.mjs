// Exemple JavaScript / TypeScript — appel de l'API StatementFlow.
// Aucune dépendance : fetch natif (Node 18+).
//
//   node examples/exemple_javascript.mjs
//
// Prérequis : lancer l'API localement :
//   uvicorn statementflow.api:app --host 127.0.0.1 --port 8080
//   (mode ouvert local : export STATEMENTFLOW_ALLOW_OPEN_AUTH=1)

const BASE = process.env.STATEMENTFLOW_URL || 'http://127.0.0.1:8080';

async function parseStatement(filePath, currencyHint) {
  const fs = await import('node:fs/promises');
  const path = await import('node:path');
  const buffer = await fs.readFile(filePath);
  const form = new FormData();
  form.append('file', new Blob([buffer]), path.basename(filePath));
  const headers = {};
  if (currencyHint) headers['X-Currency-Hint'] = currencyHint;
  if (process.env.STATEMENTFLOW_API_KEY)
    headers['X-API-Key'] = process.env.STATEMENTFLOW_API_KEY;

  // 1. soumission
  const submit = await fetch(`${BASE}/v1/analyze`, { method: 'POST', body: form, headers });
  if (!submit.ok) throw new Error(`soumission ${submit.status}: ${await submit.text()}`);
  const job = await submit.json();

  // 2. récupération (polling léger)
  for (let i = 0; i < 100; i++) {
    const res = await fetch(`${BASE}/v1/results/${job.job_id}`, { headers });
    const data = await res.json();
    if (data.status === 'done') return data;
    if (data.status === 'error') throw new Error(`analyse: ${data.error}`);
    await new Promise(r => setTimeout(r, 300));
  }
  throw new Error('expiration du délai d’attente');
}

const doc = process.argv[2] || 'tests/data/corpus/SYNTH_fr_societegenerale_A.pdf';
const result = await parseStatement(doc);
console.log(`${result.document.filename} : ${result.transactions.length} transactions`);
console.log(`  devise=${result.currency} clôture=${result.closing_balance} ` +
            `équilibré=${result.checks.opening_closing_ok}`);
for (const t of result.transactions.slice(0, 3)) {
  const sens = t.debit != null ? 'débit' : 'crédit';
  const montant = t.debit ?? t.credit;
  console.log(`  ${t.date} ${t.label.slice(0, 38).padEnd(40)} ${sens.padStart(7)} ${montant}`);
}
