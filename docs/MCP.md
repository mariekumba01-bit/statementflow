# Serveur MCP StatementFlow

[Model Context Protocol](https://modelcontextprotocol.io) — transport **stdio**, implémentation SDK officielle Python (`mcp`).

## Démarrage

```bash
pip install -e ".[mcp]"
statementflow-mcp
```

Déclaration dans un client MCP (ex. Claude Desktop, `claude_desktop_config.json`) :

```json
{
  "mcpServers": {
    "statementflow": {
      "command": "statementflow-mcp"
    }
  }
}
```

## Outils exposés

### `parse_statement`
Analyse un relevé local (PDF natif/scanné, CSV, TSV, XLSX) → résultat complet (`statementflow/v1` : transactions, contrôles, alertes, confiance).

Paramètres :
- `path` (str, requis) : chemin absolu du fichier **sur la machine du serveur** ;
- `currency_hint` (str, optionnel) : devise si elle est absente du document (ex. `XOF`).

### `get_checks`
Contrôles de cohérence seuls : chaîne des soldes, règle d'ensemble, totaux, alertes, confiance.

Paramètres : `path` (str, requis).

### `health`
Version et limites de traitement.

## Règles d'usage

- **N'utilisez ces outils que sur des documents que vous êtes autorisé à traiter** (vos relevés, ou ceux de vos clients avec leur mandat). Le serveur lit des fichiers locaux : l'agent transmet un chemin, jamais d'identifiants bancaires.
- Aucun mot de passe, OTP ou accès de compte n'est jamais demandé — le produit ne se connecte à aucune banque.
- La sortie JSON contient les données du relevé : c'est à l'agent/appelant d'en garantir la confidentialité en aval.

## Exemple (SDK client officiel)

Voir [`examples/exemple_client_mcp.py`](../examples/exemple_client_mcp.py) :
appelle `parse_statement` sur un export Wave et `get_checks` sur un relevé PDF, affiche la chaîne des soldes et la confiance.
