# StatementFlow

**Turn bank statements and mobile money exports into normalized, verified JSON transactions.**

[English](README.md) · [Français](README.fr.md)

[![CI](https://github.com/mariekumba01-bit/statementflow/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mariekumba01-bit/statementflow/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/mariekumba01-bit/statementflow/blob/main/LICENSE)

Native PDFs, scanned PDFs (OCR), CSV, TSV, XLSX → ISO dates, cleaned labels, separated
debits/credits, currencies detected when identifiable, opening/closing balances,
**accounting consistency checks** and alerts on uncertain data. Built for **French,
European and UEMOA / francophone West African formats** (FCFA amounts, Wave / Orange
Money / MTN MoMo / Moov exports — provided lawfully by their owners).

- 100% local rules engine — **no API key, no paid service, no network calls**;
- CLI, REST API, MCP server (AI agents) and Python SDK;
- **no document storage**: ephemeral in-memory processing.

> ⚠️ **Honest performance note**: the published metrics (synthetic test corpus of 32
> documents / 447 transactions) are **internal test results**, not a guarantee on your
> real statements. Test on your own documents before any production use — that is
> exactly what the consistency checks, alerts and confidence scores are for.

## Quick start

```bash
# install (dependencies: pdfplumber, openpyxl — all free)
pip install -e .

# optional, for scanned PDF OCR:
#   apt install tesseract-ocr poppler-utils

# convert a statement to JSON
statementflow parse statement.pdf --output result.json --pretty

# a whole folder of exports → one JSON per document
statementflow parse exports/ --outdir results/ --quiet

# accounting checks only
statementflow check result.json
```

```python
from statementflow import parse_file
r = parse_file('statement.pdf')
print(r['totals'], r['checks']['opening_closing_ok'], r['confidence'])
```

## Sample output (real excerpt, synthetic document)

```json
{
  "schema": "statementflow/v1",
  "document": {"filename": "SYNTH_fr_societegenerale_A.pdf",
               "kind": "pdf", "pages": 1, "method": "text",
               "parser": "generic-columns-v3", "adapter": "generic-pdf"},
  "currency": "EUR",
  "opening_balance": 3812.53,
  "closing_balance": -3319.45,
  "transactions": [
    {"index": 1, "date": "2026-01-01", "label": "CB ABONNEMENT NETFLIX",
     "debit": 859.35, "credit": null, "balance": 2953.18, "currency": "EUR",
     "confidence": 1.0,
     "trace": {"page": 1, "method": "text", "source_row": 7}}
  ],
  "checks": {"chain_checked": 14, "chain_matches": 14, "chain_ok": true,
             "opening_closing_ok": true,
             "totals": {"count": 14, "debits": 7507.2, "credits": 6375.22}},
  "alerts": [],
  "confidence": 1.0
}
```

Full engine-generated examples: [`outputs/`](outputs/) (French statement, Wave CSV
export, scanned PDF with alerts). Schema reference: [`docs/API.md`](docs/API.md).

## Self-hosting

A `Dockerfile` is provided for local or private-network use
(`docker run --rm -p 127.0.0.1:8080:8080 statementflow`). **Do not expose it
publicly as-is** — see [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for what is
ready, what needs configuration, and what must be secured first.

## The four interfaces

| Interface | Command / entry point | Use case |
|---|---|---|
| **CLI** | `statementflow parse <file\|folder>` | local processing, shell scripts |
| **REST API** | `uvicorn statementflow.api:app` → `/docs` (OpenAPI) | HTTP integrations |
| **MCP** | `statementflow-mcp` (stdio) | AI agents (Claude, Cursor…) |
| **Python SDK** | `from statementflow.sdk import Client` | Python applications |

```bash
# REST API (interactive docs at http://127.0.0.1:8080/docs)
uvicorn statementflow.api:app --host 127.0.0.1 --port 8080
curl -X POST http://127.0.0.1:8080/v1/analyze -F "file=@statement.pdf"

# MCP server (declare it in your MCP client)
statementflow-mcp
```

Ready-to-run integration examples: [`examples/`](examples/) — Python,
JavaScript/TypeScript, curl, **n8n** (importable JSON workflow), MCP client.

## What the engine does

- **Input formats**: native PDFs (pdfplumber), scanned PDFs (tesseract OCR at 300 dpi
  with automatic fallback), CSV/TSV/XLSX (separator sniffing, UTF-8 BOM, offset headers).
- **Amounts**: `1 234,56` (FR, non-breaking space), `1.234,56` (DE/ES), `1,234.56`
  (UK), `1 234 567 XOF` (integer FCFA), accounting parentheses `(750,38)`, trailing
  minus `1.234,56-`.
- **Dates**: ISO, `31/01/2026`, `31.01.2026`, `31-01-2026`, 2-digit years — always
  normalized to ISO 8601; impossible dates become `null` + alert (never invented).
- **Debit/credit**: separate columns, signed amounts, or direction inferred from a
  "Sens/Type" column (RECEPTION/ENVOI/Cash-in…) — a positive amount with no direction
  signal is marked **assumed debit + warning**.
- **Accounting checks**: line-by-line balance chain, global rule (opening + credits −
  debits = closing), duplicate handling (page repeats removed, adjacent duplicates flagged).
- **Alerts & confidence**: every transaction carries a confidence score (lowered on null
  date, missing amount, low OCR quality) and **traceability** (page, method, source
  row); every document carries coded alerts (`date_unparseable`,
  `currency_not_identified`, `chain_break`, `ocr_used`…).

## Test metrics (synthetic corpus — see [`tests/`](tests/))

| Measure | Value | Note |
|---|---|---|
| Transaction recall / precision | **100% / 100%** | 447/447 extracted, 0 invented |
| Dates | 98.0% | 9 *impossible* generator dates → `null` + alert |
| Labels, debits, credits, balances | **100%** | |
| Perfectly converted documents | 27/32 | the other 5: documented honest behaviors |
| Currency | 67% | 5 CSVs with no currency in content → `null` + alert (never invented) |

Test suite: **120 pytest tests** (units, corpus with frozen non-regression thresholds,
the 4 historical fixes, API, CLI, real JSON-RPC MCP, malformed files, accounting).
CI runs them on every push and pull request.

```bash
python -m pytest tests/ -q          # 120 passed
```

## Security & privacy

- **No storage**: bytes are processed in memory and never written to disk by the engine;
  OCR temp files live in an OS temp dir and are removed.
- API: results kept in process memory for at most `RESULTS_TTL_S` (default 15 min),
  then purged; a restart wipes everything.
- **No logging of banking data** (only filename, size, error codes).
- Strict limits: 20 MB / 200 pages / 200,000 rows by default (`STATEMENTFLOW_*` env vars).
- The product **does not ask for bank passwords, OTP codes or account access** — it only
  processes documents the user is authorized to transmit. It is **not GDPR-certified**
  and claims no regulatory compliance without an independent legal audit — see
  [`docs/SECURITY.md`](docs/SECURITY.md).

## Known limitations

1. Metrics come from a **synthetic corpus**; real statements (batch layouts, exotic
   fonts, graphical headers) can fail — alerts and confidence exist to flag it.
2. OCR confidence is capped (≤ 0.75, 0.6 if low); very low-quality scans can yield
   wrong amounts — **always check** `chain_break` / `ocr_low_confidence` alerts.
3. Currency is never guessed: documents without a readable currency → `currency: null`
   + alert (provide `--currency-hint`).
4. Tested formats: French, DE/NL/ES/UK, UEMOA (XOF), mobile money (Wave, Orange Money,
   MTN MoMo, Moov). Other banks may need an adapter ([`docs/FORMATS.md`](docs/FORMATS.md)).
5. Encrypted/password-protected PDFs are rejected with an explicit error.
6. Legacy binary `.xls` is not supported (XLSX only).

## Documentation

- [`docs/API.md`](docs/API.md) — full JSON schema, endpoints, alert codes
- [`docs/MCP.md`](docs/MCP.md) — MCP server and exposed tools
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — design, adapters, extension guide
- [`docs/FORMATS.md`](docs/FORMATS.md) — supported formats and statement templates
- [`docs/SECURITY.md`](docs/SECURITY.md) — data handling, limits, legal notes
- [`docs/BILLING.md`](docs/BILLING.md) — usage metering, quotas, future billing (no gateway integrated)
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — self-hosting: ready / needs config / must secure
- [`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md) — dependency licenses

## License

MIT — see [`LICENSE`](LICENSE).

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Golden rule: every fix ships with a
non-regression test; the test corpus is never modified to make results look better.
