"""Offline tests for the live-check runner's evidence and credential boundaries."""
import contextlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_agents
import mcp_fixture


class LiveCheckTests(unittest.TestCase):
    def test_request_summary_omits_private_content(self):
        result = test_agents.controls({'model': 'synthetic-private-deployment', 'input': 'synthetic-private-prompt',
                                      'reasoning': {'effort': 'high', 'summary': 'auto', 'text': 'synthetic-reasoning'},
                                      'thinking': {'type': 'adaptive'}, 'output_config': {'effort': 'high'}})
        self.assertEqual(result, {'reasoning': {'effort': 'high', 'summary': 'auto'},
                                  'thinking': {'type': 'adaptive'}, 'output_config': {'effort': 'high'}})
        self.assertNotIn('synthetic', json.dumps(result))

    def test_completion_requires_an_assistant_response_not_an_echoed_prompt(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / 'client.log'
            log.write_text(json.dumps({'type': 'message_end', 'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'EFFORT_CHECK_OK'}]}}))
            self.assertFalse(test_agents.completion_marker(log, 'EFFORT_CHECK_OK'))
            events = [
                {'type': 'message_end', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'EFFORT_CHECK_OK'}]}},
                {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'EFFORT_CHECK_OK'}]}},
                {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'EFFORT_CHECK_OK'}},
                {'type': 'text', 'part': {'text': 'EFFORT_CHECK_OK'}},
            ]
            for event in events:
                with self.subTest(event=event):
                    log.write_text(json.dumps(event))
                    self.assertTrue(test_agents.completion_marker(log, 'EFFORT_CHECK_OK'))

    def test_mcp_fixture_records_real_tool_and_resource_calls(self):
        requests = [
            {'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25'}},
            {'method': 'notifications/initialized'},
            {'id': 2, 'method': 'tools/list'},
            {'id': 3, 'method': 'tools/call', 'params': {'name': 'verify', 'arguments': {'nonce': 'wrong'}}},
            {'id': 4, 'method': 'tools/call', 'params': {'name': 'verify', 'arguments': {'nonce': 'coding-check'}}},
            {'id': 5, 'method': 'resources/read', 'params': {'uri': 'kondash-test://fixture'}},
        ]
        with tempfile.TemporaryDirectory() as folder:
            ledger = Path(folder) / 'ledger'
            output = io.StringIO()
            with patch('sys.stdin', io.StringIO('\n'.join(json.dumps(r) for r in requests))), contextlib.redirect_stdout(output):
                mcp_fixture.serve(ledger, 'synthetic-receipt')
            responses = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(len(responses), 5)
            self.assertTrue(responses[2]['result']['isError'])
            self.assertEqual(responses[3]['result']['content'][0]['text'], 'synthetic-receipt')
            self.assertEqual(ledger.read_text().splitlines().count('verify'), 1)
            self.assertIn('resource', ledger.read_text().splitlines())

    def sample_results(self):
        coding = {'exit': 0, 'calculation': True, 'file_write': True, 'mcp_call': True, 'mcp_receipt': True,
                  'mcp_resource': False, 'called_tools': ['edit', 'mcp__fixture__verify'], 'declared_tools': [],
                  'http_statuses': [200]}
        effort = {'low': {'exit': 0, 'completed': True, 'control_matches': True, 'controls': [], 'http_statuses': [200]},
                  'high': {'exit': 124, 'completed': False, 'control_matches': True, 'controls': [], 'http_statuses': [200]}}
        return [{'agent': 'codex', 'setup': 'passed', 'coding': coding, 'effort': {'low': effort['low']}},
                {'agent': 'pi', 'setup': 'passed', 'coding': dict(coding, mcp_receipt=False), 'effort': effort},
                {'agent': 'claude', 'setup': 'failed', 'exit': 2}]

    def test_failures_drive_both_formats(self):
        passed, failed, broken = self.sample_results()
        self.assertEqual(test_agents.failures(passed), [])
        self.assertEqual(test_agents.failures(failed), ['mcp receipt', 'effort high'])
        self.assertEqual(test_agents.failures(broken), ['setup failed'])

    def test_text_report_shows_each_check_and_summary(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            for result in self.sample_results():
                test_agents.print_result(result, Path('/tmp/report'))
            test_agents.print_summary(self.sample_results(), Path('/tmp/report'))
        text = output.getvalue()
        self.assertIn('Codex: passed', text)
        self.assertIn('Pi: FAILED', text)
        self.assertIn('x Saved the MCP receipt', text)            # plain symbols: not a terminal
        self.assertIn('high  no reply, effort sent, timed out', text)
        self.assertIn('Setup failed (exit 2)', text)
        self.assertIn('failed: mcp receipt, effort high', text)
        self.assertNotIn('\033[', text)

    def test_recording_proxy_does_not_forward_unauthenticated_requests(self):
        server = test_agents.http.server.ThreadingHTTPServer(('127.0.0.1', 0), test_agents.Recorder)
        server.key = 'synthetic-local-secret'
        server.records = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with patch.object(test_agents.urllib.request, 'build_opener') as opener:
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                connection.request('POST', '/v1/responses', '{}')
                response = connection.getresponse()
                self.assertEqual(response.status, 401)
                response.read()
                connection.close()
                opener.assert_not_called()
                self.assertEqual(server.records, [])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
