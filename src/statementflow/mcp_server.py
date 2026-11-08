"""Serveur MCP StatementFlow (Model Context Protocol).

Transport stdio. Compatible SDK mcp v1 (FastMCP) et v2 (MCPServer,
renommage de FastMCP) : la classe est importée sous le même nom local.

Outils exposés :
- parse_statement : analyse un fichier de relevé (chemin local) autorisé
  par l'utilisateur → transactions + contrôles + alertes ;
- get_checks      : contrôles de cohérence seuls d'un fichier déjà parsé
  (ou directement du document) ;
- health          : version et limites.

Aucune donnée bancaire n'est journalisée ; les chemins sont lus en local
par le serveur lui-même (l'agent transmet un chemin, pas un document).
"""
from __future__ import annotations

import os

try:  # SDK mcp v1
    from mcp.server.fastmcp import FastMCP
    _MCP_V2 = False
except ModuleNotFoundError:  # SDK mcp v2 : FastMCP renommé MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP
    _MCP_V2 = True

from . import __version__
from .config import Config
from .engine import ParseError, parse_file

# v2 expose la version du serveur via kwarg ; v1 la déduit du SDK
mcp = (FastMCP('statementflow', version=__version__) if _MCP_V2
       else FastMCP('statementflow'))

_ALLOWED_EXT = ('.pdf', '.csv', '.tsv', '.xlsx', '.xls', '.txt')
_config = Config.from_env()


@mcp.tool()
def parse_statement(path: str, currency_hint: str = '') -> dict:
    """Analyse un relevé bancaire ou un export mobile money local
    (PDF natif/scanné, CSV, TSV, XLSX) et retourne les transactions
    normalisées (dates ISO, débit/crédit, soldes), les contrôles de
    cohérence comptable et les alertes.

    N'utilisez cet outil que sur des documents que vous êtes autorisé à
    traiter. La devise n'est jamais inventée : passez currency_hint
    (ex. « XOF ») si elle est absente du document.
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        return {'error': f'Fichier introuvable : {path}'}
    if not path.lower().endswith(_ALLOWED_EXT):
        return {'error': 'Extension non supportée '
                         '(attendues : PDF, CSV, TSV, XLSX).'}
    try:
        result = parse_file(path, _config, currency_hint or None)
    except ParseError as e:
        return {'error': str(e)}
    return result


@mcp.tool()
def get_checks(path: str) -> dict:
    """Contrôles de cohérence comptable d'un relevé : chaîne des soldes,
    règle ouverture + crédits − débits = clôture, totaux, alertes."""
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        return {'error': f'Fichier introuvable : {path}'}
    try:
        result = parse_file(path, _config)
    except ParseError as e:
        return {'error': str(e)}
    return {'checks': result['checks'], 'alerts': result['alerts'],
            'confidence': result['confidence'],
            'currency': result['currency'],
            'opening_balance': result['opening_balance'],
            'closing_balance': result['closing_balance']}


@mcp.tool()
def health() -> dict:
    """État du service : version, limites de traitement."""
    return {'status': 'ok', 'version': __version__,
            'limits': {'max_file_mb': _config.max_file_mb,
                       'max_pages': _config.max_pages,
                       'max_rows': _config.max_rows}}


def main() -> None:
    mcp.run(transport='stdio')


if __name__ == '__main__':
    main()
