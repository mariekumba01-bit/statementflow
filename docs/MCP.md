# Serveur MCP distant StatementFlow

[Model Context Protocol](https://modelcontextprotocol.io) — transport **Streamable HTTP**, serveur **sans état** (stateless), implémentation native (aucune dépendance ajoutée : le serveur est monté dans l'application FastAPI existante).

Ce document remplace la version précédente, qui ne décrivait que le serveur stdio local. Les deux serveurs coexistent :

| Serveur | Transport | Moteur | Facturation | Usage |
|---|---|---|---|---|
| **Distant** (ce document) | HTTP `POST /mcp` | Celui du **service hébergé** | Crédits du compte appelant (réelle) | Agents IA connectés à l'API publique (Claude, ChatGPT, Cursor…) |
| Local (`statementflow-mcp`) | stdio | Machine de l'utilisateur | Aucune | Poste de travail, données sensibles |

Le serveur local stdio reste documenté en fin de page.

## URL de connexion

Après déploiement, l'endpoint unique est :

```
https://<votre-domaine>/mcp
```

En production StatementFlow : `https://statementflow-api-ch0f.onrender.com/mcp`

- **Uniquement `POST /mcp`** (`Content-Type: application/json`) — un message JSON-RPC 2.0 par requête, réponse `application/json`.
- `GET /mcp` (flux serveur) et `DELETE /mcp` (fin de session) répondent **405** : le serveur est stateless et n'émet pas de notifications serveur — comportement conforme à la spécification MCP pour ce mode.
- Aucun `Mcp-Session-Id` n'est émis : chaque requête est autonome ; `initialize` n'est pas exigé avant `tools/list` (compatibilité maximale des clients distants).
- Versions de protocole supportées : `2025-11-25`, `2025-06-18`, `2025-03-26`, `2024-11-05` (négociation par `initialize` ; en-tête `MCP-Protocol-Version` validé si fourni → 400 si non supportée).
- Batches JSON-RPC (tableaux) refusés (`-32600`), comme l'exige MCP.
- Capacité déclarée : **`tools` uniquement** (ni `resources`, ni `prompts`).

Méthodes : `initialize`, `ping`, `tools/list`, `tools/call`.

## Authentification et sécurité

### Jeton requis à chaque requête

```
Authorization: Bearer <clé API StatementFlow>
```

ou en-tête `X-API-Key: <clé>` (les deux sont acceptés). La clé est celle du **compte client** (préfixe `sf_…`, visible une fois sur le tableau de bord). Sans clé valide : `401` + `WWW-Authenticate: Bearer` ; clé révoquée : `401` ; aucune clé configurée sur l'instance : `503`.

La validation passe par le **même chemin que l'API REST** (`_require_key` : hash SHA-256, comparaison en temps constant, mode fermé par défaut).

### Limites assumées de cette version (honnêteté technique)

- **Ce n'est pas OAuth 2.1.** Le jeton est la clé API du compte : un secret durable qui identifie le compte entier, sans émission/expiration/rotation automatique, sans scope délégué, sans consentement par un fournisseur d'identité. Les clients qui **exigent** OAuth (certains connecteurs d'entreprise) ne pourront pas s'y connecter en l'état. Une feuille de route prévoit un fournisseur OAuth 2.1 devant ce même endpoint ; ne présentez jamais cette authentification comme équivalente.
- Rotation possible manuellement (outil `rotate_api_key`, avec confirmation explicite) ou depuis le tableau de bord web.
- Rate limiting mémoire par clé (défaut **120 req/min**, `STATEMENTFLOW_MCP_RATE_LIMIT_PER_MIN`) et par IP avant authentification (30/min) — état par processus, adapté au mono-instance Render.
- Le serveur MCP n'expose **aucune fonction d'administration** et ne peut pas en atteindre (jamais de clé admin en jeu). Aucune route existante n'a été modifiée.

### Isolation et facturation inviolables côté MCP

- Chaque outil agit **uniquement** pour la clé appelante : un job d'un autre compte est une erreur « inconnu », indistinguable d'un job inexistant.
- `convert_document` suit le **même chemin financier** que `POST /v1/analyze` : réservation atomique d'un crédit → complément au coût réel (1 crédit = 1 page PDF ou 50 lignes de tableau) → restitution intégrale sur échec. **Aucun contournement de facturation n'est possible via MCP.**
- **Idempotence robuste au redémarrage (v0.6.1)** : rejouer `convert_document` avec la même `idempotency_key` renvoie le job existant sans nouveau débit — y compris si le service a redémarré entre-temps (le job est relu depuis PostgreSQL pendant sa période de rétention).
- Aucun outil n'exécute de paiement : `buy_credits` crée la session et retourne l'URL ; le paiement reste un geste humain (mobile money), l'activation reste serveur (webhook signé + re-vérification).
- La clé API n'est **jamais retournée** dans les résultats ordinaires (masquée `sf_a****`), sauf une fois à la rotation explicitement demandée et confirmée.
- Aucun secret (Supabase, SasPay, variables Render) n'est manipulé ni exposé par le serveur MCP.

## Outils disponibles (11)

### Lecture (aucun débit)

| Outil | Description | Arguments |
|---|---|---|
| `get_account_info` | Identité du compte appelant : email, vérification, clé masquée, mode de facturation | — |
| `get_credits` | Solde de crédits et usage de la clé (documents, pages) | — |
| `get_supported_formats` | Formats d'entrée (PDF natifs/scannés, CSV, TSV, XLSX — **pas** le `.xls` binaire legacy), banques et opérateurs mobile money reconnus, structure du JSON produit | — |
| `get_limits` | Limites : taille, pages, lignes, TTL des résultats, quotas, débit MCP | — |
| `list_conversions` | Historique des conversions du compte | `limit` (1-100, défaut 20), `offset` |
| `get_conversion_status` | Statut d'un traitement : `processing`/`done`/`error` (+ usage, facturation) | `job_id` |
| `get_result` | Résultat complet : transactions normalisées, contrôles comptables, alertes | `job_id` |
| `get_pricing` | Catalogue des packs (FCFA) et règle de facturation | — |

### Actions

| Outil | Description | Confirmation |
|---|---|---|
| `convert_document` | Convertit un document (base64) via le moteur du service ; facturé aux crédits du compte ; idempotent par `idempotency_key` | — (facturation affichée dans `get_pricing`/`get_limits`) |
| `rotate_api_key` | Régénère la clé API ; l'ancienne est **révoquée immédiatement**, le solde transféré ; nouvelle clé visible une seule fois | `confirm: true` **obligatoire** |
| `buy_credits` | Initie l'achat d'un pack : crée la session de paiement et retourne `checkout_url` ; aucun prélèvement par l'outil | `confirm: true` **obligatoire** |

Annotations MCP : `readOnlyHint` sur les lectures, `destructiveHint` sur `rotate_api_key`, `idempotencyHint` sur `convert_document`.

### Erreurs structurées

Erreurs de protocole JSON-RPC : `-32700` (JSON invalide), `-32600` (requête invalide / batch), `-32601` (méthode inconnue), `-32602` (paramètres invalides), `-32603` (interne). Erreurs de transport : HTTP 401/403/413/415/429/503 avec corps `{"error": {"code": "mcp_transport_error", "message": …}}`. Erreurs métier (outil) : `tools/call` réussi avec `isError: true` et `structuredContent` (ex. `{"error": "Crédits épuisés…"}`) — l'agent peut les lire et réagir.

## Exemples d'appels (HTTP brut)

```bash
BASE=https://statementflow-api-ch0f.onrender.com/mcp
KEY=sf_votre_cle

# Découverte
curl -sS -X POST $BASE -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# Solde
curl -sS -X POST $BASE -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"get_credits","arguments":{}}}'

# Conversion (document en base64)
B64=$(base64 -w0 releve-wave.csv)
curl -sS -X POST $BASE -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",
       \"params\":{\"name\":\"convert_document\",
                   \"arguments\":{\"filename\":\"releve-wave.csv\",
                                  \"data_base64\":\"$B64\"}}}"

# Résultat complet
curl -sS -X POST $BASE -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call",
       "params":{"name":"get_result","arguments":{"job_id":"<job_id>"}}}'
```

Exemple minimal en Python (aucun SDK requis) :

```python
import json, urllib.request

BASE = "https://statementflow-api-ch0f.onrender.com/mcp"
KEY = "sf_votre_cle"

def rpc(method, params=None, id=1):
    req = urllib.request.Request(
        BASE, method="POST",
        data=json.dumps({"jsonrpc": "2.0", "id": id, "method": method,
                         **({"params": params} if params else {})}).encode(),
        headers={"Authorization": f"Bearer {KEY}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)

print(rpc("tools/list")["result"]["tools"])
credits = json.loads(rpc("tools/call", {"name": "get_credits",
                                        "arguments": {}})["result"]["content"][0]["text"])
print(credits["credits_remaining"], "crédits")
```

## Connexion des clients

### Claude (applications desktop / API Anthropic)

1. Récupérez votre clé API StatementFlow (tableau de bord, bouton « Régénérer » ou à l'inscription).
2. Dans Claude Desktop : Paramètres → Connecteurs / Extensions → **Ajouter un connecteur MCP** → URL : `https://statementflow-api-ch0f.onrender.com/mcp` → authentification par en-tête `Authorization: Bearer <clé>` (si l'interface propose « Bearer token », collez la clé ; sinon utilisez un en-tête personnalisé `Authorization` avec la valeur `Bearer <clé>`).
3. Les outils StatementFlow apparaissent après validation de la connexion ; commencez par demander « Quels outils StatementFlow sont disponibles ? » puis « Convertis ce relevé ».

Configuration avancée (fichier `claude_desktop_config.json`, si votre version le permet pour les serveurs HTTP) :

```json
{
  "mcpServers": {
    "statementflow": {
      "type": "http",
      "url": "https://statementflow-api-ch0f.onrender.com/mcp",
      "headers": { "Authorization": "Bearer sf_votre_cle" }
    }
  }
}
```

> Vérification honnête : cette configuration a été **validée par tests automatisés sur le transport** (initialize, tools/list, tools/call, notifications, 405 sur GET/DELETE — 49 tests) mais **pas encore testée dans l'interface de Claude Desktop elle-même** (aucun compte Claude n'était disponible lors du développement).

### ChatGPT (connecteurs / API OpenAI)

ChatGPT (applications et API Responses) accepte les serveurs MCP distants en tant que **connecteurs** :

1. Développeurs (API Responses) : déclarez l'outil de type MCP avec `server_url = https://statementflow-api-ch0f.onrender.com/mcp` et `authorization` = `Bearer <clé API StatementFlow>` — voir la documentation OpenAI « MCP connectors ».
2. Interface ChatGPT : Paramètres → Connecteurs → le cas échéant via une app MCP déployée sur votre espace ; l'URL et le jeton sont les mêmes.

```json
{
  "type": "mcp",
  "server_label": "statementflow",
  "server_url": "https://statementflow-api-ch0f.onrender.com/mcp",
  "require_approval": "never",
  "headers": { "Authorization": "Bearer sf_votre_cle" }
}
```

> Vérification honnête : **non testé dans l'interface ChatGPT** (pas de compte disponible) ; le serveur respecte le transport et les codes d'erreur attendus par les clients MCP modernes.

### Cursor

`~/.cursor/mcp.json` (ou Paramètres → MCP → Add new global MCP server) :

```json
{
  "mcpServers": {
    "statementflow": {
      "url": "https://statementflow-api-ch0f.onrender.com/mcp",
      "headers": { "Authorization": "Bearer sf_votre_cle" }
    }
  }
}
```

> Vérification honnête : **non testé dans Cursor** ; format de configuration conforme à la documentation Cursor pour les serveurs HTTP distants.

### Intégration via l'API Anthropic (agents)

```json
{
  "type": "mcp",
  "url": "https://statementflow-api-ch0f.onrender.com/mcp",
  "name": "statementflow",
  "authorization_token": "sf_votre_cle"
}
```

> Note : c'est la forme acceptée par l'API Anthropic (MCP server connector) ; le jeton est envoyé comme `Authorization: Bearer` par l'API.

### Intégration via l'API OpenAI (agents développés avec le SDK)

```python
from openai import OpenAI
client = OpenAI()
resp = client.responses.create(
    model="gpt-4o-mini",
    input="Convertis ce relevé et donne-moi le solde de crédits restant.",
    tools=[{
        "type": "mcp",
        "server_label": "statementflow",
        "server_url": "https://statementflow-api-ch0f.onrender.com/mcp",
        "headers": {"Authorization": "Bearer sf_votre_cle"},
    }],
)
print(resp.output_text)
```

### SDK officiel `mcp` (Python, tout agent)

```python
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

async with streamablehttp_client(
        "https://statementflow-api-ch0f.onrender.com/mcp",
        headers={"Authorization": "Bearer sf_votre_cle"}) as (r, w, _):
    async with ClientSession(r, w) as session:
        await session.initialize()
        tools = await session.list_tools()
        res = await session.call_tool("get_credits", {})
        print(res.content[0].text)
```

## Tests et CI

- `tests/test_mcp_http.py` (49 tests) : transport (initialize, versions, notifications 202, batch refusé, 415, 405, parse error), authentification (401 / WWW-Authenticate, clé révoquée), outils de lecture, **conversion réelle via le moteur avec décompte exact des crédits**, remboursement sur échec, idempotence (rejeu sans re-débit, isolation par clé), isolation entre comptes (résultat/historique/solde), actions confirmées (rotation, achat avec FakeSasPay + webhook signé), concurrence (8 appels sur 1 crédit → 1 conversion), aucune fuite de secret dans les réponses, rate limiting par clé.
- La CI (`.github/workflows/tests.yml`) exécute ces tests sur Python 3.10/3.11/3.12 à chaque push (job `tests` : `pytest tests/` avec `pip install -e ".[dev,api,mcp]"`), le job `pg-tests` couvre le mode PostgreSQL, et le job `build` vérifie la distribution.
- Aucun paiement réel : la passerelle est le simulateur `FakeSasPay` (mêmes contrats), les webhooks sont signés avec un secret de test.

## Variables d'environnement (aucune nouvelle obligatoire)

Le serveur MCP n'exige **aucune nouvelle variable** pour fonctionner : il réutilise l'authentification et les magasins déjà configurés (Render/Supabase inchangés). Optionnelle :

| Variable | Défaut | Rôle |
|---|---|---|
| `STATEMENTFLOW_MCP_RATE_LIMIT_PER_MIN` | `120` | Requêtes MCP maximales par minute et par clé authentifiée |

Les limites de taille et de rétention restent celles de `STATEMENTFLOW_MAX_FILE_MB`, `STATEMENTFLOW_RESULTS_TTL_S`, etc. (voir `docs/DEPLOYMENT.md`).

## Déploiement

Aucun changement de configuration Render n'est nécessaire : le serveur MCP est embarqué dans l'image existante (le `Dockerfile` copie `src/` et installe le paquet ; la route est servie par le même `uvicorn statementflow.api:app`). Après push sur `main`, le déploiement standard expose `/mcp` au même domaine que l'API.

Vérification post-déploiement :

```bash
curl -sS -X POST https://<domaine>/mcp -H "Authorization: Bearer sf_votre_cle" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head -c 300
```

## Feuille de route authentification

1. **V1 (livrée)** : jeton = clé API du compte, `Authorization: Bearer`. Simple, immédiatement compatible avec tous les clients acceptant des en-têtes personnalisés. Limites documentées ci-dessus.
2. **V2 (prévue)** : fournisseur OAuth 2.1 (Authorization Code + PKCE) devant le même endpoint `/mcp` : consentement utilisateur, jetons courts, scopes (`conversions:read`, `conversions:write`, `billing:initiate`), révocation par session — pour les clients exigeant OAuth nativement.

---

# Annexe : serveur local stdio (inchangé)

[Model Context Protocol](https://modelcontextprotocol.io) — transport **stdio**, implémentation SDK officielle Python (`mcp`).

## Démarrage

```bash
pip install -e ".[mcp]"
statementflow-mcp
```

Déclaration dans un client MCP (ex. Claude Desktop, `claude_desktop_config.json`) :
pointez `command` sur l'exécutable du venv où StatementFlow est installé :

```json
{
  "mcpServers": {
    "statementflow": {
      "command": "/chemin/vers/venv/bin/statementflow-mcp"
    }
  }
}
```

Compatibilité SDK : le serveur fonctionne avec le SDK `mcp` v1 (`FastMCP`)
comme v2 (`MCPServer`, renommage upstream) — shim d'import depuis v0.2.0.

## Outils exposés (stdio)

### `parse_statement`
Analyse un relevé local (PDF natif/scanné, CSV, TSV, XLSX ; `.txt` traité comme CSV) → résultat complet (`statementflow/v1` : transactions, contrôles, alertes, confiance). Le `.xls` binaire legacy n'est pas supporté (erreur explicite).

Paramètres :
- `path` (str, requis) : chemin absolu du fichier **sur la machine du serveur** ;
- `currency_hint` (str, optionnel) : devise si elle est absente du document (ex. `XOF`).

### `get_checks`
Contrôles de cohérence seuls : chaîne des soldes, règle d'ensemble, totaux, alertes, confiance.

Paramètres : `path` (str, requis).

### `health`
Version et limites de traitement.

## Règles d'usage

- **N'utilisez ces outils que sur des documents que vous êtes autorisés à traiter** (vos relevés, ou ceux de vos clients avec leur mandat). Le serveur lit des fichiers locaux : l'agent transmet un chemin, jamais d'identifiants bancaires.
- Aucun mot de passe, OTP ou accès de compte n'est jamais demandé — le produit ne se connecte à aucune banque.
- La sortie JSON contient les données du relevé : c'est à l'agent/appelant d'en garantir la confidentialité en aval.

## Exemple (SDK client officiel)

Voir [`examples/exemple_client_mcp.py`](../examples/exemple_client_mcp.py) :
appelle `parse_statement` sur un export Wave et `get_checks` sur un relevé PDF, affiche la chaîne des soldes et la confiance.
