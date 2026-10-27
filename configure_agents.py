#!/usr/bin/env python3
"""Add the local KONDash gateway to installed coding agents without changing defaults."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from terminal import fail, headline, ok, skip, step, table, tidy, warn

from install import PROXY_SUFFIX, ROOT, NoRedirect, SetupError, atomic_write

AGENTS = ("opencode", "pi", "codex", "claude")
LABELS = {"opencode": "OpenCode", "pi": "Pi", "codex": "Codex", "claude": "Claude Code"}
PROVIDER = "kondash"
OWNER = "kondash-cliproxyapi-agents"
MISSING = object()
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')


def digest(value):
    data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def safe_path(path):
    path = Path(path).expanduser().resolve()
    if path == ROOT or ROOT in path.parents:
        raise SetupError("Generated client configuration and credentials must stay outside this checkout.")
    return path


def mask_jsonc(text):
    tokens = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/')
    comments = tokens.sub(lambda match: ''.join('\n' if c == '\n' else ' ' for c in match.group())
                          if match.group().startswith('/') else match.group(), text)
    clean = re.sub(r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])',
                   lambda match: ' ' if match.group() == ',' else match.group(), comments)
    return comments, clean


def parse_jsonc(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    try:
        result = json.loads(mask_jsonc(text)[1], object_pairs_hook=unique,
                            parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Invalid constant")))
    except ValueError:
        raise SetupError("An existing JSON/JSONC configuration is invalid or has duplicate keys; no files changed.") from None
    if not isinstance(result, dict):
        raise SetupError("Agent configuration must be a JSON object.")
    return result


def jsonc_objects(text):
    clean = mask_jsonc(text)[1]
    decoder = json.JSONDecoder()
    objects = {}

    def skip(index):
        while index < len(clean) and clean[index].isspace():
            index += 1
        return index

    def walk(index, path):
        index = skip(index)
        if clean[index] != '{':
            return decoder.raw_decode(clean, index)[1]
        start = index
        index = skip(index + 1)
        members = {}
        while clean[index] != '}':
            key_start = index
            key, index = decoder.raw_decode(clean, index)
            index = skip(index)
            value_start = skip(index + 1)
            value_end = walk(value_start, path + (key,))
            members[key] = (key_start, value_start, value_end)
            index = skip(value_end)
            if clean[index] == ',':
                index = skip(index + 1)
        objects[path] = {"start": start, "close": index, "members": members}
        return index + 1

    walk(0, ())
    return objects


def line_indent(text, index):
    line = text[text.rfind('\n', 0, index) + 1:index]
    return re.match(r'[ \t]*', line).group()


def upsert_jsonc(text, path, value):
    """Edit only the requested object member, keeping unrelated comments and formatting."""
    parse_jsonc(text)
    objects = jsonc_objects(text)
    parent = objects.get(path[:-1])
    if parent is None:
        return upsert_jsonc(text, path[:-1], {path[-1]: value})
    slot = parent["members"].get(path[-1])
    if slot:
        key_start, start, end = slot
        indent = line_indent(text, key_start)
        rendered = json.dumps(value, indent=2, ensure_ascii=False).replace('\n', '\n' + indent)
        result = text[:start] + rendered + text[end:]
    else:
        close = parent["close"]
        trailing_comma = False
        if parent["members"]:
            last = max(slot[2] for slot in parent["members"].values())
            trailing_comma = ',' in mask_jsonc(text)[0][last:close]
            if not trailing_comma:
                text = text[:last] + ',' + text[last:]
                close += 1
        indent = line_indent(text, parent["start"])
        child_indent = indent + '  '
        rendered = json.dumps(value, indent=2, ensure_ascii=False).replace('\n', '\n' + child_indent)
        head = text[:close].rstrip(' \t')
        if not head.endswith('\n'):
            head += '\n'
        result = (head + child_indent + json.dumps(path[-1]) + ': ' + rendered
                  + (',' if trailing_comma else '') + '\n' + indent + text[close:])
    parse_jsonc(result)
    return result


def get_member(document, path):
    for part in path:
        if not isinstance(document, dict):
            raise SetupError("An agent provider/settings section must be an object; no files changed.")
        if part not in document:
            return MISSING
        document = document[part]
    return document


class Plan:
    def __init__(self, state_dir, replace=False):
        self.state_dir = safe_path(state_dir)
        self.manifest_path = self.state_dir / "manifest.json"
        self.before_manifest = self.manifest_path.read_bytes() if self.manifest_path.exists() else None
        self.state = json.loads(self.before_manifest) if self.before_manifest else {"owner": OWNER, "entries": {}, "files": {}}
        if (not isinstance(self.state, dict) or self.state.get("owner") != OWNER
                or not isinstance(self.state.get('entries'), dict) or not isinstance(self.state.get('files'), dict)):
            raise SetupError("Existing agent-setup metadata is not owned by this script.")
        self.state = copy.deepcopy(self.state)
        self.replace = replace
        self.changes = {}

    def original(self, path):
        path = safe_path(path)
        if path not in self.changes:
            old = path.read_bytes() if path.exists() else None
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
            self.changes[path] = {"before": old, "after": old, "mode": mode, "target_mode": 0o600}
        return path, self.changes[path]

    def entry(self, path, member_path, value):
        path, change = self.original(path)
        text = change["after"].decode() if change["after"] is not None else '{}\n'
        current = get_member(parse_jsonc(text), member_path)
        identity = str(path) + '::' + json.dumps(member_path)
        if (current is not MISSING and current != value and not self.replace
                and self.state["entries"].get(identity) != digest(current)):
            raise SetupError("The KONDash provider/auth entry already exists or was edited.",
                             "Run again with --replace; the current file is backed up first.")
        if current is MISSING or current != value:
            change["after"] = upsert_jsonc(text, member_path, value).encode()
        self.state["entries"][identity] = digest(value)

    def file(self, path, text, mode=0o600):
        path, change = self.original(path)
        proposed = text.encode()
        if (change["before"] is not None and change["before"] != proposed and not self.replace
                and self.state["files"].get(str(path)) != digest(change["before"])):
            raise SetupError("A generated KONDash profile/helper already exists or was edited.",
                             "Run again with --replace; the current file is backed up first.")
        change["after"] = proposed
        change["target_mode"] = mode
        self.state["files"][str(path)] = digest(proposed)

    def apply(self, dry_run=False):
        changes = {path: change for path, change in self.changes.items()
                   if change["before"] != change["after"] or change["mode"] != change["target_mode"]}
        for path in self.changes:
            change = changes.get(path)
            if change is None:
                skip("Unchanged " + tidy(path))
            elif change["before"] is None:
                ok(("Would create " if dry_run else "Created ") + tidy(path))
            elif change["before"] != change["after"]:
                ok(("Would update " if dry_run else "Updated ") + tidy(path))
            else:
                ok(("Would fix permissions on " if dry_run else "Fixed permissions on ") + tidy(path))
        if dry_run:
            return
        for path, change in self.changes.items():
            current = path.read_bytes() if path.exists() else None
            if current != change["before"]:
                raise SetupError("A client configuration changed during setup; retry without concurrent edits.")
        current_manifest = self.manifest_path.read_bytes() if self.manifest_path.exists() else None
        if current_manifest != self.before_manifest:
            raise SetupError("Another client-setup process changed the manifest; retry.")
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir.chmod(0o700)
        backup = None
        if any(change["before"] is not None for change in changes.values()):
            backups = self.state_dir / "backups"
            backups.mkdir(mode=0o700, exist_ok=True)
            backup = Path(tempfile.mkdtemp(prefix="before-", dir=backups))
            paths = {}
            for index, (path, change) in enumerate(changes.items()):
                if change["before"] is not None:
                    name = str(index) + '-' + path.name
                    atomic_write(backup / name, change["before"])
                    paths[name] = {"path": str(path), "mode": change["mode"]}
            atomic_write(backup / 'restore-paths.json', json.dumps(paths, indent=2) + '\n')
        applied = []
        try:
            for path, change in changes.items():
                atomic_write(path, change["after"], change["target_mode"])
                applied.append(path)
            atomic_write(self.manifest_path, json.dumps(self.state, indent=2) + '\n')
        except OSError:
            for path in reversed(applied):
                change = changes[path]
                if change["before"] is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, change["before"], change["mode"])
            raise SetupError("Writing client configuration failed; applied client changes were rolled back.") from None
        if backup:
            ok("Previous versions backed up to " + tidy(backup))


def run_json(arguments):
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise ValueError()
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise SetupError("Cannot read the Codex bundled model catalog.",
                         "Update or reinstall Codex, or pass a catalog with --codex-catalog FILE.") from None


def client_version(executable):
    try:
        result = subprocess.run([executable, '--version'], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        raise SetupError("Cannot run " + executable + " --version.",
                         "Check that the agent works on its own, or reinstall it.") from None
    match = re.search(r'(?<!\d)(\d+)\.(\d+)\.(\d+)\b', result.stdout)
    if result.returncode or not match:
        raise SetupError(executable + " --version did not print a version number.",
                         "Check that the agent works on its own, or reinstall it.")
    return tuple(map(int, match.groups()))


def gateway_models(base_url, key_file):
    url = urllib.parse.urlsplit(base_url)
    if (url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1')
            or url.username or url.password or url.query or url.fragment or not url.path.endswith('/v1')):
        raise SetupError("The gateway URL must be an HTTP localhost API URL ending in /v1.")
    try:
        key = key_file.read_text().strip()
    except OSError:
        raise SetupError("The local gateway is not installed (no proxy key found).",
                         "Install it first: python3 install.py") from None
    if not key or any(c.isspace() for c in key):
        raise SetupError("The local proxy-key file is invalid; contents withheld.",
                         "Recreate it: delete " + tidy(key_file) + ", then run python3 install.py")
    request = urllib.request.Request(base_url + '/models', headers={'Authorization': 'Bearer ' + key})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            document = json.load(response)
        ids = {item['id'] for item in document['data'] if isinstance(item.get('id'), str)
               and item['id'] and not any(ord(c) < 32 for c in item['id'])}
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        raise SetupError(f"The local gateway rejected the request (HTTP {code}); response body withheld.",
                         "The proxy key and gateway may not match; run python3 install.py again."
                         if code in (401, 403) else "See: journalctl --user -u cliproxyapi.service -n 50") from None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise SetupError("Cannot reach the local gateway at " + base_url, "Start it: systemctl --user start cliproxyapi.service\nNot installed yet? Run: python3 install.py") from None
    models = sorted(model for model in ids if model + PROXY_SUFFIX in ids)
    if not models:
        raise SetupError("The local gateway does not offer the expected KONDash models.",
                         "Refresh its configuration: python3 install.py --reconfigure")
    return models


def prepare_codex_catalog(executable, models, supplied=None):
    try:
        catalog = json.loads(supplied.read_text()) if supplied else run_json([executable, 'debug', 'models', '--bundled'])
    except (OSError, ValueError):
        raise SetupError("Cannot read the supplied Codex model catalog.") from None
    if not isinstance(catalog, dict) or not isinstance(catalog.get('models'), list):
        raise SetupError("Codex catalog must contain a models array.")
    selected = [copy.deepcopy(model) for model in catalog['models']
                if isinstance(model, dict) and model.get('slug') in models]
    if not selected:
        raise SetupError("Codex has no metadata matching these deployments.",
                         "Pass a catalog with --codex-catalog FILE, or leave Codex out with --agents (e.g. --agents pi claude).")
    for model in selected:
        limits = [model.get(name) for name in ('context_window', 'max_context_window')]
        efforts = model.get('supported_reasoning_levels', [])
        if (any(limit is not None and (type(limit) is not int or limit <= 0) for limit in limits)
                or not isinstance(efforts, list) or any(not isinstance(entry, dict) for entry in efforts)):
            raise SetupError("The selected Codex model metadata has invalid limits or reasoning levels.")
        model.update(tool_mode='direct', use_responses_lite=False, supports_reasoning_effort_updates=False,
                     node_repl_disabled=True, experimental_supported_tools=[], supports_search_tool=False,
                     context_window=min(model.get('context_window') or 128000, 128000),
                     max_context_window=min(model.get('max_context_window') or 128000, 128000),
                     service_tiers=[], additional_speed_tiers=[], default_service_tier=None, upgrade=None,
                     input_modalities=['text'])
    return {'models': selected}


def configure(args):
    config_root = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    gateway_dir = safe_path(config_root / 'cliproxyapi')
    key_file = safe_path(args.proxy_key_file or gateway_dir / 'proxy-key')
    port = 8317
    metadata = gateway_dir / 'kondash-install.json'
    if metadata.exists() and not args.base_url:
        installed = json.loads(metadata.read_text())
        if not isinstance(installed, dict):
            raise SetupError("The gateway installation metadata must be a JSON object.")
        port = installed.get('port', port)
        if type(port) is not int or not 1 <= port <= 65535:
            raise SetupError("The gateway installation metadata has an invalid local port.")
    base_url = (args.base_url or f'http://127.0.0.1:{port}/v1').rstrip('/')
    step("Connecting to the local gateway")
    models = gateway_models(base_url, key_file)
    ok(f"{base_url} answered: {len(models)} models available")
    step("Looking for coding agents")
    requested = AGENTS if args.agents == ['all'] else args.agents
    executables = {name: shutil.which(name) for name in requested}
    if args.agents != ['all'] and any(not path for path in executables.values()):
        missing = [LABELS[name] for name, path in executables.items() if not path]
        raise SetupError("Not found on PATH: " + ", ".join(missing),
                         "Install it, or add its folder to PATH (check with: which "
                         + " ".join(name for name, path in executables.items() if not path) + ").")
    for name, path in executables.items():
        if not path:
            skip(LABELS[name] + " is not installed")
    executables = {name: path for name, path in executables.items() if path}
    if not executables:
        raise SetupError("No supported coding agents were found on PATH.",
                         "Install at least one of: " + ", ".join(LABELS.values()) + ".\n"
                         "Already installed? Make sure its folder is on PATH, then run this again.")
    versions = {name: client_version(path) for name, path in executables.items()}

    def version(name):
        return '.'.join(map(str, versions[name]))

    for name, path in executables.items():
        ok(LABELS[name] + ' ' + version(name) + ' (' + tidy(path) + ')')

    def unsupported(name, message, fix):
        # Explicit requests fail; with the default 'all', skip just this agent.
        if args.agents != ['all']:
            raise SetupError(message, fix)
        warn('Skipping ' + LABELS[name] + ' ' + version(name) + ': ' + message, fix)
        del executables[name]
        if not executables:
            raise SetupError("No compatible coding agents are left to configure.", "Follow the steps above, then run this again.")

    if 'codex' in executables and versions['codex'] < (0, 164, 0):
        unsupported('codex', "Automatic Codex catalog/profile setup requires Codex 0.164.0 or newer.",
                    "Update Codex to 0.164.0 or newer, then run this again.")
    catalog = None
    if 'codex' in executables:
        catalog = prepare_codex_catalog(executables['codex'], models, args.codex_catalog)
    selected = args.model or (catalog['models'][0]['slug'] if catalog else models[0])
    if selected not in models:
        raise SetupError("The requested model is not available through the local gateway.",
                         "Leave out --model to use the default, or pick a model listed in the web UI.")
    if catalog and selected not in {model['slug'] for model in catalog['models']}:
        raise SetupError("The selected model has no matching Codex metadata; supply --codex-catalog or select another model.")
    plan = Plan(gateway_dir / 'agents', args.replace)
    reasoning = plan.state.setdefault('reasoning', {})
    if not isinstance(reasoning, dict):
        raise SetupError('The private reasoning preferences must be an object.')
    requested_efforts = getattr(args, 'reasoning_efforts', None)
    if requested_efforts is not None:
        reasoning[selected] = list(dict.fromkeys(requested_efforts))
    for levels in reasoning.values():
        if not isinstance(levels, list) or any(level not in EFFORTS for level in levels):
            raise SetupError('The private reasoning preferences contain unsupported levels.')
    # Thinking controls are on by default; an explicitly stored list (possibly empty) overrides it.
    efforts = {model: reasoning.get(model, list(EFFORTS)) for model in models}
    if ('opencode' in executables and versions['opencode'][0] < 2
            and any(reasoning.get(model) for model in models)):
        unsupported('opencode', 'Explicit reasoning setup requires OpenCode v2.',
                    'Update OpenCode to v2, or turn reasoning off with --reasoning-efforts (no values).')
    cat = shutil.which('cat')
    if not cat:
        raise SetupError("The cat command was not found.", "Install coreutils with your package manager.")
    credential_command = shlex.join([cat, str(key_file)])
    if 'opencode' in executables:
        folder = safe_path(config_root / 'opencode')
        path = folder / ('opencode.jsonc' if (folder / 'opencode.jsonc').exists() else 'opencode.json')
        modern = versions['opencode'][0] >= 2
        oc_efforts = efforts if modern else {}
        runtime = {'baseURL': base_url, 'apiKey': '{file:' + str(key_file) + '}'}
        provider = {'name': 'KONDash',
                    'package' if modern else 'npm': '@opencode/ai/providers/openai-compatible/responses' if modern else '@ai-sdk/openai-compatible',
                    'settings' if modern else 'options': runtime,
                    'models': {model: {'name': 'KONDash ' + model,
                               **({'capabilities': {'tools': True, 'input': ['text'], 'output': ['text']}} if modern else {}),
                               **({'capabilities': {'tools': True, 'input': ['text'], 'output': ['text'],
                                                   'reasoning': {'supported': True, 'effort': oc_efforts[model]}},
                                   'variants': [{'id': level, 'settings': {'reasoningEffort': level}}
                                                for level in oc_efforts[model]]} if oc_efforts.get(model) else {}),
                               'limit': {'context': 128000, 'output': 8192}} for model in models}}
        plan.entry(path, ('providers' if modern else 'provider', PROVIDER), provider)
    if 'pi' in executables:
        folder = safe_path(os.environ.get('PI_CODING_AGENT_DIR', Path.home() / '.pi/agent'))
        provider = {'name': 'KONDash', 'baseUrl': base_url, 'api': 'openai-responses',
                    'apiKey': '!' + credential_command, 'authHeader': True,
                    'models': [{'id': model, 'name': 'KONDash ' + model, 'reasoning': bool(efforts[model]),
                                **({'thinkingLevelMap': {level: level if level in efforts[model] else None
                                                        for level in ('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')}}
                                   if efforts[model] else {}),
                                'input': ['text'], 'contextWindow': 128000, 'maxTokens': 8192} for model in models]}
        plan.entry(folder / 'models.json', ('providers', PROVIDER), provider)
        auth = folder / 'auth.json'
        if auth.exists() and PROVIDER in parse_jsonc(auth.read_text()):
            plan.entry(auth, (PROVIDER,), {'type': 'api_key', 'key': '!' + credential_command})
    if 'codex' in executables:
        folder = safe_path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
        catalog_path = gateway_dir / 'agents/codex-models.json'
        # Codex uses the translated route, so its catalog slugs carry the route suffix.
        routed = {'models': [dict(model, slug=model['slug'] + PROXY_SUFFIX) for model in catalog['models']]}
        plan.file(catalog_path, json.dumps(routed, indent=2) + '\n')
        selected_info = next(model for model in catalog['models'] if model['slug'] == selected)
        efforts = {entry.get('effort') for entry in selected_info.get('supported_reasoning_levels', [])}
        profile = ('# Managed by ' + OWNER + '\n'
                   'model = ' + json.dumps(selected + PROXY_SUFFIX) + '\n'
                   'model_provider = "' + PROVIDER + '"\n'
                   'model_catalog_json = ' + json.dumps(str(catalog_path)) + '\n'
                   + ('model_reasoning_effort = "low"\n' if 'low' in efforts else '')
                   + 'web_search = "disabled"\n\n'
                   '[model_providers.' + PROVIDER + ']\nname = "KONDash"\n'
                   'base_url = ' + json.dumps(base_url) + '\nwire_api = "responses"\n\n'
                   '[model_providers.' + PROVIDER + '.auth]\ncommand = ' + json.dumps(cat) + '\n'
                   'args = ' + json.dumps([str(key_file)]) + '\n\n'
                   '[model_providers.' + PROVIDER + '.capabilities]\nremote_compaction = "unsupported"\n')
        plan.file(folder / (PROVIDER + '.config.toml'), profile)
    if 'claude' in executables:
        settings = gateway_dir / 'agents/claude.settings.json'
        plan.file(settings, json.dumps({'model': selected, 'apiKeyHelper': credential_command,
                  'env': {'ANTHROPIC_BASE_URL': base_url[:-3],
                          'ANTHROPIC_DEFAULT_SONNET_MODEL': selected,
                          'ANTHROPIC_DEFAULT_OPUS_MODEL': selected,
                          'ANTHROPIC_DEFAULT_HAIKU_MODEL': selected,
                          'CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY': '1'}}, indent=2) + '\n')
        launcher = Path.home() / '.local/bin/claude-kondash'
        if launcher.is_symlink():
            raise SetupError("~/.local/bin/claude-kondash already exists as a symlink.",
                             "Move or delete it, then run this again.")
        shell = ('#!/bin/sh\n# Managed by ' + OWNER + '\n'
                 'unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_OAUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN\n'
                 'unset ANTHROPIC_MODEL ANTHROPIC_SMALL_FAST_MODEL\n'
                 'unset CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX CLAUDE_CODE_USE_FOUNDRY CLAUDE_CODE_USE_MANTLE\n'
                 'exec ' + shlex.join([executables['claude'], '--settings', str(settings)]) + ' "$@"\n')
        plan.file(launcher, shell, 0o700)
    step("Planned changes (dry run: nothing is written)" if args.dry_run else "Writing agent configuration")
    plan.apply(args.dry_run)
    if args.dry_run:
        headline("Dry run complete. Run again without --dry-run to apply.")
        return
    headline("Configured " + ", ".join(LABELS[name] for name in executables) + ".")
    print("Your own defaults and other providers were left as they were. Model IDs are not printed.")
    print()
    usage = {'opencode': 'restart OpenCode, then choose KONDash in /models',
             'pi': 'choose KONDash in /model',
             'codex': 'codex --profile ' + PROVIDER,
             'claude': '~/.local/bin/claude-kondash'}
    table([(LABELS[name], usage[name]) for name in executables])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agents', nargs='+', choices=('all',) + AGENTS, default=['all'])
    parser.add_argument('--base-url', help='Override the localhost /v1 URL; otherwise use the installed gateway port')
    parser.add_argument('--proxy-key-file', type=Path, help='Override the private local proxy-key file')
    parser.add_argument('--model', help='Choose the default model for the separate Codex/Claude profiles')
    parser.add_argument('--codex-catalog', type=Path, help='Use a private Codex catalog for deployment aliases absent from bundled metadata')
    parser.add_argument('--reasoning-efforts', nargs='*', choices=EFFORTS,
                        help='Limit the selected model to these Pi/OpenCode v2 reasoning levels (default: all); remembered privately. Pass no levels to disable.')
    parser.add_argument('--replace', action='store_true', help='Back up and replace conflicting or edited KONDash entries/profiles')
    parser.add_argument('--dry-run', action='store_true', help='Preflight and report planned changes without writing files')
    args = parser.parse_args()
    if 'all' in args.agents and args.agents != ['all']:
        parser.error('Use --agents all alone, or name individual agents')
    args.agents = list(dict.fromkeys(args.agents))
    try:
        configure(args)
    except (SetupError, OSError, ValueError, RecursionError) as error:
        print()
        if isinstance(error, SetupError):
            fail('Agent setup failed: ' + str(error), error.fix)
        else:
            fail('Agent setup failed: cannot read or update client configuration; private details withheld.',
                 'Check that your agent config files are readable and your home directory is writable.')
        return 1
    except KeyboardInterrupt:
        print()
        fail('Agent setup cancelled.')
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
