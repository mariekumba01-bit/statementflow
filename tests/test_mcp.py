"""Tests pytest du serveur MCP par JSON-RPC stdio réel (subprocess)."""
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, 'data', 'corpus')


@pytest.fixture()
def mcp():
    proc = subprocess.Popen(
        [sys.executable, '-m', 'statementflow.mcp_server'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True,
        cwd=os.path.dirname(HERE))
    yield MCPClient(proc)
    proc.stdin.close()
    proc.wait(timeout=10)


class MCPClient:
    def __init__(self, proc):
        self.proc = proc
        self._id = 0

    def request(self, method, params):
        self._id += 1
        self.proc.stdin.write(json.dumps(
            {'jsonrpc': '2.0', 'id': self._id, 'method': method,
             'params': params}) + '\n')
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def notify(self, method):
        self.proc.stdin.write(json.dumps(
            {'jsonrpc': '2.0', 'method': method}) + '\n')
        self.proc.stdin.flush()

    def call_tool(self, name, args):
        r = self.request('tools/call', {'name': name, 'arguments': args})
        return json.loads(r['result']['content'][0]['text'])


class TestMCP:
    def test_initialize_et_tools(self, mcp):
        r = mcp.request('initialize', {
            'protocolVersion': '2024-11-05', 'capabilities': {},
            'clientInfo': {'name': 'pytest', 'version': '0'}})
        assert r['result']['serverInfo']['name'] == 'statementflow'
        mcp.notify('notifications/initialized')
        tools = [t['name'] for t in mcp.request('tools/list', {})['result']['tools']]
        assert {'parse_statement', 'get_checks', 'health'} <= set(tools)

    def test_parse_statement(self, mcp):
        mcp.notify('notifications/initialized')
        data = mcp.call_tool('parse_statement', {
            'path': os.path.join(CORPUS, 'SYNTH_csv_wave_v2_U.csv')})
        assert len(data['transactions']) == 12
        assert data['currency'] == 'XOF'
        assert data['document']['adapter'] == 'wave-csv'

    def test_parse_statement_pdf(self, mcp):
        mcp.notify('notifications/initialized')
        data = mcp.call_tool('parse_statement', {
            'path': os.path.join(CORPUS, 'SYNTH_fr_societegenerale_A.pdf')})
        assert len(data['transactions']) == 14
        assert data['checks']['chain_ok'] is True

    def test_get_checks(self, mcp):
        mcp.notify('notifications/initialized')
        data = mcp.call_tool('get_checks', {
            'path': os.path.join(CORPUS, 'SYNTH_fr_societegenerale_A.pdf')})
        assert data['checks']['opening_closing_ok'] is True
        assert 'confidence' in data

    def test_health(self, mcp):
        mcp.notify('notifications/initialized')
        data = mcp.call_tool('health', {})
        assert data['status'] == 'ok'

    def test_erreur_propre_fichier_inexistant(self, mcp):
        mcp.notify('notifications/initialized')
        data = mcp.call_tool('parse_statement', {'path': '/tmp/inexistant.pdf'})
        assert 'error' in data

    def test_erreur_extension(self, mcp, tmp_path):
        mcp.notify('notifications/initialized')
        bad = tmp_path / 'x.exe'
        bad.write_text('nope')
        data = mcp.call_tool('parse_statement', {'path': str(bad)})
        assert 'error' in data
