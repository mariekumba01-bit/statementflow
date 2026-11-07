#!/usr/bin/env python3
"""Test du serveur MCP par JSON-RPC stdio réel (sans client MCP)."""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

proc = subprocess.Popen(
    [sys.executable, '-m', 'statementflow.mcp_server'],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, cwd=ROOT)

msgid = 0


def send(method, params):
    global msgid
    msgid += 1
    req = {'jsonrpc': '2.0', 'id': msgid, 'method': method, 'params': params}
    proc.stdin.write(json.dumps(req) + '\n')
    proc.stdin.flush()
    # lire une ligne de réponse (les notifications n'en produisent pas)
    line = proc.stdout.readline()
    return json.loads(line)


# 1. initialize
r = send('initialize', {
    'protocolVersion': '2024-11-05',
    'capabilities': {},
    'clientInfo': {'name': 'test-client', 'version': '0.0.1'}})
print('initialize →', r['result']['serverInfo'])

# notification initialized (pas de réponse attendue)
proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n')
proc.stdin.flush()

# 2. tools/list
r = send('tools/list', {})
tools = [t['name'] for t in r['result']['tools']]
print('tools/list →', tools)
assert 'parse_statement' in tools and 'get_checks' in tools and 'health' in tools

# 3. health
r = send('tools/call', {'name': 'health', 'arguments': {}})
content = r['result']['content'][0]['text']
print('health →', json.loads(content))

# 4. parse_statement sur un document du corpus
r = send('tools/call', {'name': 'parse_statement', 'arguments': {
    'path': os.path.join(ROOT, 'tests', 'data', 'corpus', 'SYNTH_csv_wave_v2_U.csv')}})
data = json.loads(r['result']['content'][0]['text'])
print('parse_statement →', len(data['transactions']), 'tx | devise:',
      data['currency'], '| adapter:', data['document']['adapter'])
assert len(data['transactions']) == 12
assert data['currency'] == 'XOF'

# 5. get_checks
r = send('tools/call', {'name': 'get_checks', 'arguments': {
    'path': os.path.join(ROOT, 'tests', 'data', 'corpus', 'SYNTH_fr_societegenerale_A.pdf')}})
data = json.loads(r['result']['content'][0]['text'])
print('get_checks →', data['checks']['chain_matches'], '/',
      data['checks']['chain_checked'], '| équilibre:', data['checks']['opening_closing_ok'])

# 6. fichier inexistant → erreur propre (pas de crash)
r = send('tools/call', {'name': 'parse_statement', 'arguments': {
    'path': '/tmp/inexistant.pdf'}})
data = json.loads(r['result']['content'][0]['text'])
print('fichier inexistant →', data)
assert 'error' in data

proc.stdin.close()
proc.wait(timeout=10)
print('MCP JSON-RPC : TOUS LES TESTS PASSENT')
