#!/usr/bin/env python3
"""Opt-in live coding/MCP/effort checks; uses paid gateway requests and isolated homes."""
import argparse
import concurrent.futures
import http.server
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

import configure_agents as setup
from terminal import bad, fail, headline, ok, skip, step, table, warn

ROOT = Path(__file__).resolve().parent


def controls(body):
    """Retain control fields, never input text, model IDs, keys, or reasoning text."""
    result = {}
    for key in ('reasoning', 'thinking', 'output_config'):
        if isinstance(body.get(key), dict):
            result[key] = {k: v for k, v in body[key].items()
                           if k in ('effort', 'summary', 'type', 'budget_tokens') and isinstance(v, (str, int, bool))}
    return result


class Recorder(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def handle_request(self):
        if (self.headers.get('Authorization') != 'Bearer ' + self.server.key
                and self.headers.get('x-api-key') != self.server.key):
            self.send_error(401, 'Local test credential required')
            return
        length = int(self.headers.get('Content-Length', 0))
        data = self.rfile.read(length) if length else None
        body = json.loads(data) if data else {}
        entry = {'path': self.path.split('?')[0], 'controls': controls(body), 'declared_tools': [], 'called_tools': []}
        for tool in body.get('tools', []):
            name = tool.get('name') or tool.get('function', {}).get('name')
            if name:
                entry['declared_tools'].append(name)
        self.server.records.append(entry)
        request = urllib.request.Request(self.server.upstream + self.path, data=data,
                                         method=self.command, headers={'Authorization': 'Bearer ' + self.server.key,
                                         'Content-Type': 'application/json'})
        for key in ('anthropic-version', 'anthropic-beta'):
            if self.headers.get(key):
                request.add_header(key, self.headers[key])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), setup.NoRedirect())
        pending = b''
        try:
            try:
                response = opener.open(request, timeout=180)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                entry['status'] = response.code
                self.send_response(response.code)
                self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
                self.end_headers()
                while True:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    pending += chunk
                    while b'\n' in pending:
                        line, pending = pending.split(b'\n', 1)
                        if line.startswith(b'data: '):
                            try:
                                event = json.loads(line[6:])
                                item = event.get('item') or event.get('content_block') or {}
                                if item.get('type') in ('function_call', 'custom_tool_call', 'tool_use') and item.get('name'):
                                    entry['called_tools'].append(item['name'])
                                if 'reasoning' in event.get('type', '') or item.get('type') in ('reasoning', 'thinking'):
                                    entry['reasoning_events'] = True
                                usage = event.get('response', {}).get('usage') or event.get('usage') or {}
                                details = usage.get('output_tokens_details') or {}
                                if isinstance(details.get('reasoning_tokens'), int):
                                    entry['reasoning_tokens'] = details['reasoning_tokens']
                            except (ValueError, AttributeError):
                                pass
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (OSError, ValueError):
            entry['transport_error'] = True

    do_GET = handle_request
    do_POST = handle_request


def execute(arguments, env, cwd, timeout, log):
    with log.open('w') as stream:
        process = subprocess.Popen(arguments, env=env, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return 124


def isolated_env(home):
    env = {'HOME': str(home), 'PATH': os.environ['PATH'], 'LANG': 'C.UTF-8', 'TERM': 'dumb',
           'CODEX_HOME': str(home / 'codex'), 'PI_CODING_AGENT_DIR': str(home / 'pi'),
           'CLAUDE_CONFIG_DIR': str(home / 'claude'), 'PI_OFFLINE': '1', 'PI_TELEMETRY': '0',
           'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'OPENCODE_DISABLE_MODELS_FETCH': 'true',
           'OPENCODE_DISABLE_AUTOUPDATE': 'true', 'OPENCODE_DISABLE_FREE_MODELS': 'true'}
    for kind in ('CONFIG', 'DATA', 'CACHE', 'STATE', 'RUNTIME'):
        folder = home / kind.lower()
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        env['XDG_' + ('RUNTIME_DIR' if kind == 'RUNTIME' else kind + '_HOME')] = str(folder)
    return env


def completion_marker(log, marker):
    for line in log.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get('message') or {}
        if message.get('role') == 'assistant' and event.get('type') in ('message_end', 'assistant'):
            if any(block.get('type') == 'text' and marker in block.get('text', '') for block in message.get('content', [])):
                return True
        item = event.get('item') or {}
        if item.get('type') == 'agent_message' and marker in item.get('text', ''):
            return True
        if event.get('type') == 'text' and marker in event.get('part', {}).get('text', ''):
            return True
    return False


def run_agent(name, executable, model, key_file, gateway, destination, args):
    home = destination / name
    home.mkdir(mode=0o700)
    env = isolated_env(home)
    workspace = home / 'workspace'
    workspace.mkdir()
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Recorder)
    server.upstream = gateway[:-3]
    server.key = key_file.read_text().strip()
    server.records = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f'http://127.0.0.1:{server.server_port}/v1'
        command = [sys.executable, str(ROOT / 'configure_agents.py'), '--agents', name,
                   '--base-url', base, '--proxy-key-file', str(key_file), '--model', model]
        if args.codex_catalog:
            command += ['--codex-catalog', str(args.codex_catalog)]
        if args.enable_reasoning and name in ('pi', 'opencode'):
            levels = [level for level in args.efforts if level in ('low', 'medium', 'high', 'xhigh', 'max')]
            command += ['--reasoning-efforts'] + (levels or ['low', 'high'])
        code = execute(command, env, workspace, 45, home / 'setup.log')
        if code:
            return {'agent': name, 'setup': 'failed', 'exit': code}
        ledger = home / 'mcp-ledger'
        receipt = 'RECEIPT_' + secrets.token_hex(12)
        fixture = [sys.executable, str(ROOT / 'tests/mcp_fixture.py'), str(ledger), receipt]
        mcp = {'command': fixture[0], 'args': fixture[1:]}
        if name == 'codex':
            profile = home / ('codex/' + setup.PROVIDER + '.config.toml')
            with profile.open('a') as stream:
                stream.write('\n[mcp_servers.fixture]\ncommand = ' + json.dumps(fixture[0]) + '\nargs = ' + json.dumps(fixture[1:]) + '\n')
                stream.write('default_tools_approval_mode = "approve"\n')
        elif name == 'pi':
            (home / 'pi/mcp.json').write_text(json.dumps({'mcpServers': {'fixture': dict(mcp, exposure='direct')}}))
        elif name == 'opencode':
            path = home / 'config/opencode/opencode.json'
            config = json.loads(path.read_text())
            config['mcp'] = {'servers': {'fixture': {'type': 'local', 'command': fixture}}}
            path.write_text(json.dumps(config))
        else:
            (home / 'mcp.json').write_text(json.dumps({'mcpServers': {'fixture': mcp}}))
        (workspace / 'calculator.py').write_text('# SYNTHETIC_FIND_ME\ndef add(a, b):\n    return a - b\n')
        (workspace / 'check.py').write_text('from calculator import add\nassert add(7,5)==12\nprint("CODING_ASSERTION_OK")\n')
        prompt = ('Only work in the current workspace. Do not access other files, the network, credentials, or delegate to other agents. '
                  'Use native coding tools to list files, find calculator.py, search SYNTHETIC_FIND_ME, read it, and edit add to return a + b. '
                  'Use a native write tool to create note.txt containing exactly CODING_FILE_OK followed by a newline. '
                  'Run python3 check.py using the terminal tool and fix failures. '
                  'Call the fixture MCP verify tool with nonce coding-check; do not imitate it or read its server files. '
                  'Write only the returned receipt text to receipt.txt. Read the MCP resource kondash-test://fixture if the client exposes resource reading. '
                  'Finish by replying CODING_TOOLS_OK. Use the native read/edit/write/search/list tools where available, not shell substitutes for everything.')
        def invocation(effort=None, coding=False):
            text = prompt if coding else 'Reply with exactly EFFORT_CHECK_OK. Do not use tools or delegate.'
            if name == 'codex':
                cmd = [executable, '--no-daemon', 'exec', '--profile', setup.PROVIDER, '--ephemeral', '--skip-git-repo-check',
                       '--sandbox', 'workspace-write', '-c', 'approval_policy="never"', '--json']
                if effort:
                    cmd += ['-c', 'model_reasoning_effort=' + json.dumps(effort)]
            elif name == 'pi':
                cmd = [executable, '--offline', '--no-skills', '--no-context-files', '--no-prompt-templates',
                       '--no-session', '--provider', setup.PROVIDER, '--model', model, '--mode', 'json', '-p']
                if coding:
                    cmd += ['--extension', 'builtin:mcp', '--tools', 'read,write,edit,bash,grep,find,ls,mcp__fixture__*,list_mcp_resources,read_mcp_resource']
                else:
                    cmd += ['--no-tools', '--no-mcp']
                if effort:
                    cmd += ['--thinking', effort]
            elif name == 'opencode':
                cmd = [executable, 'run', '--standalone', '--auto', '--format', 'json', '--model',
                       setup.PROVIDER + '/' + model + ('#' + effort if effort else '')]
            else:
                cmd = [str(home / '.local/bin/claude-kondash'), '--bare', '--no-session-persistence', '--strict-mcp-config',
                       '--mcp-config', str(home / 'mcp.json'), '--permission-mode', 'acceptEdits',
                       '--allowedTools', 'Read,Edit,Write,Glob,Grep,Bash(python3 check.py),mcp__fixture__*',
                       '--output-format', 'stream-json', '--verbose', '-p']
                if effort:
                    cmd += ['--effort', effort]
            return cmd + [text]
        result = {'agent': name, 'setup': 'passed'}
        if args.phase in ('coding', 'all'):
            start = len(server.records)
            code = execute(invocation(coding=True), env, workspace, args.timeout, home / 'coding.log')
            records = server.records[start:]
            check = subprocess.run([sys.executable, '-c', 'import runpy; m=runpy.run_path("calculator.py"); '
                                    'assert m["add"](7,5)==12; assert m["add"](-3,5)==2'],
                                    cwd=workspace, capture_output=True, timeout=10)
            result['coding'] = {'exit': code, 'calculation': check.returncode == 0,
                                'file_write': (workspace / 'note.txt').exists() and (workspace / 'note.txt').read_text() == 'CODING_FILE_OK\n',
                                'mcp_call': ledger.exists() and 'verify' in ledger.read_text().splitlines(),
                                'mcp_receipt': (workspace / 'receipt.txt').exists() and (workspace / 'receipt.txt').read_text().strip() == receipt,
                                'mcp_resource': ledger.exists() and 'resource' in ledger.read_text().splitlines(),
                                'called_tools': sorted({t for r in records for t in r['called_tools']}),
                                'declared_tools': sorted({t for r in records for t in r['declared_tools']}),
                                'http_statuses': sorted({r['status'] for r in records if 'status' in r})}
        if args.phase in ('effort', 'all'):
            result['effort'] = {}
            for effort in args.efforts:
                start = len(server.records)
                code = execute(invocation(effort), env, workspace, args.timeout, home / ('effort-' + effort + '.log'))
                records = server.records[start:]
                sent = [r['controls'] for r in records if r['path'] in ('/v1/responses', '/v1/messages')]
                result['effort'][effort] = {'exit': code, 'completed': completion_marker(home / ('effort-' + effort + '.log'), 'EFFORT_CHECK_OK'),
                                            'control_matches': any(c.get('reasoning', {}).get('effort') == effort or
                                                                   c.get('output_config', {}).get('effort') == effort or
                                                                   (effort == 'off' and not c.get('reasoning') and not c.get('thinking')) for c in sent),
                                            'controls': [r['controls'] for r in records if r['path'] in ('/v1/responses', '/v1/messages')],
                                            'http_statuses': sorted({r['status'] for r in records if 'status' in r})}
        (home / 'wire-summary.json').write_text(json.dumps(server.records, indent=2))
        return result
    finally:
        server.shutdown()
        server.server_close()


CODING_CHECKS = (('calculation', 'Fixed calculator.py so check.py passes'),
                 ('file_write', 'Wrote note.txt with a native write tool'),
                 ('mcp_call', 'Called the fixture MCP tool'),
                 ('mcp_receipt', 'Saved the MCP receipt'))


def failures(result):
    """Short labels for every failed check; an empty list means the agent passed."""
    if result.get('setup') != 'passed':
        return ['setup ' + result.get('setup', 'error')]
    found = [key.replace('_', ' ') for key, _ in CODING_CHECKS if not result.get('coding', {}).get(key, True)]
    found += ['effort ' + level for level, value in result.get('effort', {}).items()
              if not (value['exit'] == 0 and value['completed'] and value['control_matches'])]
    return found


def exit_note(code):
    return {0: 'exit 0', 124: 'timed out'}.get(code, f'exit {code}')


def print_result(result, destination):
    name = result['agent']
    problems = failures(result)
    step(setup.LABELS[name] + (': passed' if not problems else ': FAILED'))
    if result.get('setup') != 'passed':
        detail = exit_note(result['exit']) if 'exit' in result else result.get('error_type', 'error')
        bad(f'Setup {result.get("setup")} ({detail}); see {destination / name}')
        return
    ok('Isolated profile configured')
    coding = result.get('coding')
    if coding:
        print('  Coding tools (' + exit_note(coding['exit']) + ')')
        for key, label in CODING_CHECKS:
            (ok if coding[key] else bad)(label, '    ')
        (ok if coding['mcp_resource'] else skip)(
            'Read the MCP resource' + ('' if coding['mcp_resource'] else ' (optional; the client may not offer it)'), '    ')
        skip('Tools called: ' + (', '.join(coding['called_tools']) or 'none'), '    ')
        skip('HTTP status: ' + (', '.join(map(str, coding['http_statuses'])) or 'no requests'), '    ')
    if result.get('effort'):
        print('  Reasoning effort')
        width = max(map(len, result['effort']))
        for level, value in result['effort'].items():
            notes = ['completed' if value['completed'] else 'no reply',
                     'effort sent' if value['control_matches'] else 'effort NOT sent',
                     exit_note(value['exit']),
                     'HTTP ' + (', '.join(map(str, value['http_statuses'])) or 'none')]
            passed = value['exit'] == 0 and value['completed'] and value['control_matches']
            (ok if passed else bad)(level.ljust(width) + '  ' + ', '.join(notes), '    ')


def print_summary(results, destination):
    headline('All checks passed.' if all(not failures(r) for r in results) else 'Some checks failed.')
    print()
    table([(setup.LABELS[r['agent']], 'passed' if not failures(r) else 'failed: ' + ', '.join(failures(r)))
           for r in sorted(results, key=lambda r: setup.AGENTS.index(r['agent']))])
    print()
    print('Reports and agent logs (private, remove when done): ' + str(destination))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agents', nargs='+', choices=setup.AGENTS, default=list(setup.AGENTS))
    parser.add_argument('--base-url', default='http://127.0.0.1:8317/v1')
    parser.add_argument('--proxy-key-file', type=Path)
    parser.add_argument('--model')
    parser.add_argument('--codex-catalog', type=Path)
    parser.add_argument('--phase', choices=('coding', 'effort', 'all'), default='all')
    parser.add_argument('--efforts', nargs='+', choices=('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'), default=['low', 'high'])
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--enable-reasoning', action='store_true', help='Enable the requested reasoning levels in isolated Pi/OpenCode profiles')
    parser.add_argument('--format', choices=('text', 'json'), default='text',
                        help='text: readable report (default); json: one JSON object per agent on stdout')
    args = parser.parse_args()
    text = args.format == 'text'
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    missing = [name for name in args.agents if not shutil.which(name)]
    if missing:
        raise setup.SetupError('Not found on PATH: ' + ', '.join(setup.LABELS[name] for name in missing),
                               'Install them, or leave them out with --agents (e.g. --agents '
                               + ' '.join(name for name in args.agents if name not in missing) + ').')
    if text:
        step('Preparing live agent checks')
    key_file = setup.safe_path(args.proxy_key_file or Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'cliproxyapi/proxy-key')
    models = setup.gateway_models(args.base_url, key_file)
    if text:
        ok(f'Gateway {args.base_url} answered: {len(models)} models available')
    catalog = setup.run_json([shutil.which('codex'), 'debug', 'models', '--bundled']) if shutil.which('codex') else {'models': []}
    matches = [m['slug'] for m in catalog['models'] if m['slug'] in models]
    model = args.model or (matches[0] if matches else models[0])
    if model not in models:
        raise setup.SetupError('Requested model is unavailable.', 'Leave out --model to use the default, or pick a model listed in the web UI.')
    destination = Path(tempfile.mkdtemp(prefix='kondash-agent-check-'))
    agents = list(dict.fromkeys(args.agents))
    if text:
        ok('Test model selected (ID not printed)')
        ok('Agents: ' + ', '.join(setup.LABELS[name] for name in agents))
        phases = (['coding tools'] if args.phase in ('coding', 'all') else []) + (
            ['effort ' + ' '.join(args.efforts)] if args.phase in ('effort', 'all') else [])
        ok('Checks: ' + '; '.join(phases))
        skip('Private reports: ' + str(destination))
        warn('These are real, potentially billable model requests.')
        print(f'\nRunning {len(agents)} agent(s) in parallel; each step may take up to {args.timeout}s...', flush=True)
    else:
        print('Live checks use real gateway requests. Private temporary reports: ' + str(destination), file=sys.stderr, flush=True)
    results = []
    # Each client has its own home, workspace and recording proxy.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {pool.submit(run_agent, name, shutil.which(name), model, key_file, args.base_url, destination, args): name
                for name in agents}
        for job in concurrent.futures.as_completed(jobs):
            name = jobs[job]
            try:
                result = job.result()
            except Exception as error:
                # Raw errors and client output may contain deployment IDs. Keep them private.
                (destination / (name + '-error.txt')).write_text(repr(error))
                result = {'agent': name, 'setup': 'error', 'error_type': type(error).__name__}
            results.append(result)
            if text:
                print_result(result, destination)
                sys.stdout.flush()
            else:
                print(json.dumps(result), flush=True)
    (destination / 'results.json').write_text(json.dumps(results, indent=2))
    if text:
        print_summary(results, destination)
    return 0 if results and all(not failures(r) for r in results) else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (setup.SetupError, OSError, ValueError) as error:
        print()
        if isinstance(error, setup.SetupError):
            fail('Live checks failed: ' + str(error), error.fix)
        else:
            fail('Live checks failed: cannot run checks; private details withheld.')
        sys.exit(1)
    except KeyboardInterrupt:
        print()
        fail('Live checks cancelled.', 'Partial agent logs stay in the private report folder printed above.')
        sys.exit(130)
