#!/usr/bin/env python3
"""Exemple de client MCP StatementFlow (SDK officiel mcp, stdio).

    pip install mcp
    python examples/exemple_client_mcp.py

Montre comment un agent IA consomme les outils parse_statement / get_checks.
"""
import asyncio

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(
        command='python3', args=['-m', 'statementflow.mcp_server'])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print('Outils disponibles :', [t.name for t in tools.tools])

            r = await session.call_tool('parse_statement', {
                'path': 'tests/data/corpus/SYNTH_csv_wave_v2_U.csv'})
            import json
            data = json.loads(r.content[0].text)
            print(f"Wave : {len(data['transactions'])} transactions, "
                  f"devise {data['currency']}, "
                  f"chaîne {data['checks']['chain_matches']}/"
                  f"{data['checks']['chain_checked']}")

            r = await session.call_tool('get_checks', {
                'path': 'tests/data/corpus/SYNTH_fr_societegenerale_A.pdf'})
            data = json.loads(r.content[0].text)
            print('Contrôles SG :', data['checks']['opening_closing_ok'],
                  '| confiance', data['confidence'])


if __name__ == '__main__':
    asyncio.run(main())
