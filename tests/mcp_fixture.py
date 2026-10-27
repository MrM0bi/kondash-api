#!/usr/bin/env python3
"""Small stdio MCP server used only by the isolated agent compatibility check."""
import json
from pathlib import Path
import sys


def serve(ledger, receipt):
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get('method')
        with ledger.open('a') as stream:
            stream.write('method:' + str(method) + '\n')
        if 'id' not in request:
            continue
        if method == 'initialize':
            result = {'protocolVersion': request['params']['protocolVersion'],
                      'capabilities': {'tools': {}, 'resources': {}},
                      'serverInfo': {'name': 'kondash-test-fixture', 'version': '1.0.0'}}
        elif method == 'ping':
            result = {}
        elif method == 'tools/list':
            result = {'tools': [{'name': 'verify', 'description': 'Return a verification receipt for nonce coding-check.',
                                'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False},
                                'inputSchema': {'type': 'object', 'properties': {'nonce': {'type': 'string'}},
                                                'required': ['nonce'], 'additionalProperties': False}}]}
        elif method == 'tools/call':
            valid = request['params'].get('name') == 'verify' and request['params'].get('arguments') == {'nonce': 'coding-check'}
            result = {'content': [{'type': 'text', 'text': receipt if valid else 'Invalid test arguments'}], 'isError': not valid}
            if valid:
                with ledger.open('a') as stream:
                    stream.write('verify\n')
        elif method == 'resources/list':
            result = {'resources': [{'uri': 'kondash-test://fixture', 'name': 'test-resource', 'mimeType': 'text/plain'}]}
        elif method == 'resources/templates/list':
            result = {'resourceTemplates': []}
        elif method == 'resources/read':
            result = {'contents': [{'uri': 'kondash-test://fixture', 'mimeType': 'text/plain', 'text': 'SYNTHETIC_RESOURCE_OK'}]}
            with ledger.open('a') as stream:
                stream.write('resource\n')
        elif method == 'prompts/list':
            result = {'prompts': []}
        else:
            print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32601, 'message': 'Unknown method'}}), flush=True)
            continue
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)


if __name__ == '__main__':
    serve(Path(sys.argv[1]), sys.argv[2])
