"""Client setup tests use isolated homes, synthetic model names and credentials."""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

import configure_agents as setup


class JsoncTests(unittest.TestCase):
    def test_merges_nested_entry_preserving_comments_and_other_values(self):
        text = '''{
  // Keep my defaults
  "model": "other/model",
  "providers": {
    "other": {"url": "https://example.test/a//b", "label": "/*literal*/",}, // keep
  },
}'''
        value = {"models": ["example-coder"], "enabled": True}
        result = setup.upsert_jsonc(text, ('providers', 'kondash'), value)
        data = setup.parse_jsonc(result)
        self.assertEqual(data['providers']['kondash'], value)
        self.assertEqual(data['model'], 'other/model')
        self.assertIn('// Keep my defaults', result)
        self.assertIn('// keep', result)
        self.assertEqual(data['providers']['other']['label'], '/*literal*/')

    def test_missing_parent_inline_and_commented_objects(self):
        for text in ('{}', '{/* comment */}', '{"model":"other"}',
                     '{"providers":{}}', '{"providers":{"old":{},}}'):
            with self.subTest(text=text):
                result = setup.upsert_jsonc(text, ('providers', 'kondash'), {'url': 'example'})
                self.assertEqual(setup.parse_jsonc(result)['providers']['kondash']['url'], 'example')

    def test_replace_changes_only_member(self):
        text = '{"providers":{"kondash":{"x":[1,2]},"other":{}},"model":"other"}'
        result = setup.upsert_jsonc(text, ('providers', 'kondash'), {'x': [3]})
        self.assertTrue(result.endswith(',"other":{}},"model":"other"}'))

    def test_rejects_invalid_and_duplicate_json(self):
        for text in ('[]', '{"a":1,"a":2}', '{"a":NaN}', '{"a":'):
            with self.subTest(text=text), self.assertRaises(setup.SetupError):
                setup.parse_jsonc(text)


class SetupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='client-setup-test-')
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name) / "home with 'quotes'"
        self.home.mkdir()
        self.config = self.home / '.config'
        self.gateway = self.config / 'cliproxyapi'
        self.gateway.mkdir(parents=True)
        self.key = self.gateway / 'proxy-key'
        self.key.write_text('synthetic-local-secret\n')
        self.env = {'XDG_CONFIG_HOME': str(self.config), 'CODEX_HOME': str(self.home / '.codex'),
                    'PI_CODING_AGENT_DIR': str(self.home / '.pi/agent')}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, self.env).start()
        patch.object(setup.Path, 'home', return_value=self.home).start()
        patch.object(setup.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name).start()
        self.models = ['example-coder', 'example-fast']
        patch.object(setup, 'gateway_models', return_value=self.models).start()
        patch.object(setup, 'client_version', side_effect=lambda path: (0, 164, 0) if path.endswith('codex') else (2, 2, 0)).start()
        self.catalog = {'models': [{'slug': 'example-coder', 'base_instructions': 'Synthetic model instructions',
                                   'tool_mode': 'code_mode_only', 'context_window': 272000,
                                   'apply_patch_tool_type': 'freeform',
                                   'supported_reasoning_levels': [{'effort': 'low'}]}]}
        patch.object(setup, 'run_json', return_value=self.catalog).start()
        self.args = SimpleNamespace(agents=['all'], base_url=None, proxy_key_file=None, model=None,
                                    codex_catalog=None, replace=False, dry_run=False)

    def run_setup(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            setup.configure(self.args)
        self.assertNotIn('synthetic-local-secret', output.getvalue())
        self.assertNotIn('example-coder', output.getvalue())
        return output.getvalue()

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_all_agents_preserve_existing_defaults_and_use_key_references(self):
        opencode = self.write(self.config / 'opencode/opencode.jsonc',
                              '{// my defaults\n"model":"other/model","providers":{"other":{}}}')
        pi = self.write(self.home / '.pi/agent/models.json', '{"providers":{"other":{"api":"other"}}}')
        defaults = self.write(self.home / '.codex/config.toml', 'model = "other"\n')
        claude = self.write(self.home / '.claude/settings.json', '{"model":"other"}')
        self.run_setup()
        oc = setup.parse_jsonc(opencode.read_text())
        self.assertNotIn('mcp', oc)
        self.assertEqual(oc['model'], 'other/model')
        self.assertIn('// my defaults', opencode.read_text())
        provider = oc['providers']['kondash']
        self.assertEqual(provider['package'], '@opencode/ai/providers/openai-compatible/responses')
        self.assertEqual(provider['name'], 'KONDash')
        self.assertEqual(provider['settings']['apiKey'], '{file:' + str(self.key) + '}')
        self.assertEqual(set(provider['models']), {'example-coder', 'example-fast'})
        pc = json.loads(pi.read_text())['providers']
        self.assertIn('other', pc)
        self.assertEqual(pc['kondash']['name'], 'KONDash')
        self.assertEqual(shlex.split(pc['kondash']['apiKey'][1:]), ['/usr/bin/cat', str(self.key)])
        self.assertEqual(defaults.read_text(), 'model = "other"\n')
        profile = (self.home / '.codex/kondash.config.toml').read_text()
        self.assertIn('wire_api = "responses"', profile)
        self.assertIn('name = "KONDash"', profile)
        generated_catalog = json.loads((self.gateway / 'agents/codex-models.json').read_text())
        model = generated_catalog['models'][0]
        self.assertEqual(model['slug'], 'example-coder-proxy')
        self.assertEqual(model['base_instructions'], 'Synthetic model instructions')
        self.assertEqual(model['tool_mode'], 'direct')
        self.assertEqual(model['context_window'], 128000)
        self.assertEqual(model['apply_patch_tool_type'], 'freeform')
        self.assertEqual(claude.read_text(), '{"model":"other"}')
        settings = json.loads((self.gateway / 'agents/claude.settings.json').read_text())
        self.assertEqual(settings['model'], 'example-coder')
        self.assertEqual(settings['env']['ANTHROPIC_BASE_URL'], 'http://127.0.0.1:8317')
        launcher = self.home / '.local/bin/claude-kondash'
        subprocess.run(['/bin/sh', '-n', str(launcher)], check=True)
        self.assertEqual(stat.S_IMODE(launcher.stat().st_mode), 0o700)
        for path in (opencode, pi, self.gateway / 'agents/manifest.json'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotIn('synthetic-local-secret', profile)

    def test_idempotence_and_owned_model_refresh(self):
        self.run_setup()
        backups = self.gateway / 'agents/backups'
        self.assertFalse(backups.exists())
        output = self.run_setup()
        self.assertIn('Unchanged ~/.codex/kondash.config.toml', output)
        self.assertNotIn('Updated', output)
        self.assertFalse(backups.exists())
        self.models.append('example-new')
        self.run_setup()
        self.assertEqual(len(list(backups.glob('before-*'))), 1)
        self.assertEqual(stat.S_IMODE(next(backups.glob('before-*')).stat().st_mode), 0o700)

    def test_reasoning_defaults_on_and_per_model_choice_is_remembered(self):
        self.args.reasoning_efforts = ['low', 'high', 'high']
        self.run_setup()
        def configs():
            pi = json.loads((self.home / '.pi/agent/models.json').read_text())['providers']['kondash']['models']
            oc = json.loads((self.config / 'opencode/opencode.json').read_text())['providers']['kondash']['models']
            return {m['id']: m for m in pi}, oc
        pi, oc = configs()
        self.assertTrue(pi['example-coder']['reasoning'])
        self.assertEqual(pi['example-coder']['thinkingLevelMap']['high'], 'high')
        self.assertIsNone(pi['example-coder']['thinkingLevelMap']['minimal'])
        self.assertIsNone(pi['example-coder']['thinkingLevelMap']['off'])
        self.assertTrue(pi['example-fast']['reasoning'])  # default: all levels
        self.assertEqual(oc['example-fast']['capabilities']['reasoning']['effort'], list(setup.EFFORTS))
        self.assertEqual(oc['example-coder']['capabilities']['reasoning']['effort'], ['low', 'high'])
        self.assertEqual([v['id'] for v in oc['example-coder']['variants']], ['low', 'high'])
        self.args.reasoning_efforts = None
        self.run_setup()
        self.assertTrue(configs()[0]['example-coder']['reasoning'])
        self.args.reasoning_efforts = []
        self.run_setup()
        self.assertFalse(configs()[0]['example-coder']['reasoning'])

    def test_reasoning_opt_in_on_opencode_v1_fails_before_writes(self):
        self.args.agents = ['opencode']
        self.args.reasoning_efforts = ['low', 'high']
        with patch.object(setup, 'client_version', return_value=(1, 2, 27)):
            with self.assertRaisesRegex(setup.SetupError, 'OpenCode v2'):
                self.run_setup()
        self.assertFalse((self.config / 'opencode').exists())

    def test_all_skips_incompatible_agents_but_configures_the_rest(self):
        self.args.reasoning_efforts = ['low']
        with patch.object(setup, 'client_version', side_effect=lambda path: (0, 1, 0) if path.endswith('codex') else
                          (1, 2, 27) if path.endswith('opencode') else (2, 2, 0)), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            output = self.run_setup()
        self.assertIn('Skipping Codex 0.1.0', errors.getvalue())
        self.assertIn('Skipping OpenCode 1.2.27', errors.getvalue())
        self.assertIn('Configured Pi, Claude Code.', output)
        self.assertFalse((self.home / '.codex').exists())
        self.assertFalse((self.config / 'opencode').exists())
        self.assertTrue((self.home / '.pi/agent/models.json').exists())

    def test_explicit_old_codex_still_fails(self):
        self.args.agents = ['codex', 'pi']
        with patch.object(setup, 'client_version', return_value=(0, 1, 0)):
            with self.assertRaisesRegex(setup.SetupError, '0.164.0'):
                self.run_setup()
        self.assertFalse((self.home / '.pi').exists())

    def test_dry_run_writes_nothing(self):
        self.args.dry_run = True
        self.run_setup()
        self.assertFalse((self.gateway / 'agents').exists())
        self.assertFalse((self.home / '.codex').exists())
        self.assertFalse((self.config / 'opencode').exists())

    def test_claude_launcher_passes_arguments_and_clears_competing_environment(self):
        self.args.agents = ['claude']
        executable = self.write(self.home / 'mock claude',
                                 '#!/usr/bin/python3\nimport json, os, sys\n'
                                 'print(json.dumps({"args":sys.argv[1:], "model":os.getenv("ANTHROPIC_MODEL"), '
                                 '"key":os.getenv("ANTHROPIC_API_KEY"), "bedrock":os.getenv("CLAUDE_CODE_USE_BEDROCK")}))\n')
        executable.chmod(0o700)
        with patch.object(setup.shutil, 'which', side_effect=lambda name: str(executable) if name == 'claude' else '/usr/bin/' + name):
            self.run_setup()
        env = dict(os.environ, ANTHROPIC_MODEL='synthetic-stale', ANTHROPIC_API_KEY='synthetic-stale', CLAUDE_CODE_USE_BEDROCK='1')
        result = subprocess.run([str(self.home / '.local/bin/claude-kondash'), '-p', 'argument with "quotes"'],
                                env=env, capture_output=True, text=True, check=True)
        child = json.loads(result.stdout)
        self.assertEqual(child['args'], ['--settings', str(self.gateway / 'agents/claude.settings.json'), '-p', 'argument with "quotes"'])
        self.assertIsNone(child['model'])
        self.assertIsNone(child['key'])
        self.assertIsNone(child['bedrock'])

    def test_conflicting_entry_prevents_all_writes_and_replace_backs_up(self):
        path = self.write(self.home / '.pi/agent/models.json', '{"providers":{"kondash":{"mine":true}}}')
        before = path.read_bytes()
        with self.assertRaises(setup.SetupError) as caught:
            self.run_setup()
        self.assertIn('--replace', caught.exception.fix)
        self.assertFalse((self.config / 'opencode').exists())
        self.assertEqual(path.read_bytes(), before)
        self.args.replace = True
        self.run_setup()
        backup = next((self.gateway / 'agents/backups').glob('before-*'))
        self.assertTrue(any(p.read_bytes() == before for p in backup.iterdir()))

    def test_user_edited_generated_file_is_not_overwritten(self):
        self.run_setup()
        path = self.home / '.codex/kondash.config.toml'
        path.write_text(path.read_text() + '# my edits\n')
        with self.assertRaises(setup.SetupError) as caught:
            self.run_setup()
        self.assertIn('--replace', caught.exception.fix)
        self.assertTrue(path.read_text().endswith('# my edits\n'))

    def test_unknown_codex_model_fails_preflight(self):
        self.models[:] = ['example-unknown']
        with self.assertRaises(setup.SetupError) as caught:
            self.run_setup()
        self.assertIn('--codex-catalog', caught.exception.fix)
        self.assertFalse((self.config / 'opencode').exists())

    def test_invalid_catalog_metadata_is_rejected_before_writes(self):
        self.catalog['models'][0]['context_window'] = 'invalid'
        with self.assertRaisesRegex(setup.SetupError, 'invalid limits'):
            self.run_setup()
        self.assertFalse((self.config / 'opencode').exists())

    def test_custom_gateway_port_from_private_metadata(self):
        self.write(self.gateway / 'kondash-install.json', '{"port":8456}')
        self.args.agents = ['pi']
        self.run_setup()
        provider = json.loads((self.home / '.pi/agent/models.json').read_text())['providers']['kondash']
        self.assertEqual(provider['baseUrl'], 'http://127.0.0.1:8456/v1')

    def test_invalid_install_metadata_is_rejected_before_writes(self):
        for value in ('[]', '{"port":"invalid"}', '{"port":99999}'):
            with self.subTest(value=value):
                self.write(self.gateway / 'kondash-install.json', value)
                with self.assertRaises(setup.SetupError):
                    self.run_setup()
        self.assertFalse((self.config / 'opencode').exists())

    def test_supplied_codex_catalog_selects_deployment_alias(self):
        self.models[:] = ['example-alias']
        self.catalog['models'][0]['slug'] = 'example-alias'
        self.args.codex_catalog = self.write(self.home / 'private-catalog.json', json.dumps(self.catalog))
        self.run_setup()
        self.assertIn('model = "example-alias-proxy"', (self.home / '.codex/kondash.config.toml').read_text())

    def test_kova_mcp_preserves_settings_and_is_idempotent(self):
        self.args.kova_mcp = True
        path = self.write(self.config / 'opencode/opencode.jsonc',
                          '{// keep MCP settings\n"mcp":{"timeout":{"startup":45000},'
                          '"servers":{"other":{"type":"local","command":["other"]}}}}')
        self.run_setup()
        data = setup.parse_jsonc(path.read_text())
        self.assertEqual(data['mcp']['servers']['kova'],
                         {'type': 'local', 'command': ['kova', '--mcp']})
        self.assertEqual(data['mcp']['servers']['other']['command'], ['other'])
        self.assertEqual(data['mcp']['timeout'], {'startup': 45000})
        self.assertIn('// keep MCP settings', path.read_text())
        self.assertNotIn('Updated', self.run_setup())
        self.args.kova_mcp = False
        self.run_setup()
        self.assertEqual(setup.parse_jsonc(path.read_text())['mcp'], data['mcp'])
        for filename in (self.home / '.pi/agent/models.json', self.home / '.codex/kondash.config.toml',
                         self.gateway / 'agents/claude.settings.json'):
            self.assertNotIn('kova', filename.read_text())

    def test_kova_mcp_conflict_requires_replace_and_backup(self):
        self.args.agents = ['opencode']
        self.args.kova_mcp = True
        original = '{"mcp":{"servers":{"kova":{"type":"local","command":["custom"]}}}}'
        path = self.write(self.config / 'opencode/opencode.json', original)
        with self.assertRaises(setup.SetupError) as caught:
            self.run_setup()
        self.assertIn('--replace', caught.exception.fix)
        self.assertEqual(path.read_text(), original)
        self.assertFalse((self.gateway / 'agents').exists())
        self.args.replace = True
        self.run_setup()
        self.assertEqual(json.loads(path.read_text())['mcp']['servers']['kova']['command'], ['kova', '--mcp'])
        backups = list((self.gateway / 'agents/backups').glob('before-*/*-opencode.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)

    def test_kova_mcp_dry_run_writes_nothing(self):
        self.args.kova_mcp = True
        self.args.dry_run = True
        self.run_setup()
        self.assertFalse((self.config / 'opencode').exists())
        self.assertFalse((self.gateway / 'agents').exists())

    def test_kova_mcp_ignored_without_opencode(self):
        self.args.agents = ['pi']
        self.args.kova_mcp = True
        self.run_setup()
        self.assertFalse((self.config / 'opencode').exists())
        self.assertNotIn('kova', (self.home / '.pi/agent/models.json').read_text())

    def test_kova_mcp_v1_schema(self):
        self.args.agents = ['opencode']
        self.args.kova_mcp = True
        with patch.object(setup, 'client_version', return_value=(1, 2, 27)):
            self.run_setup()
        data = json.loads((self.config / 'opencode/opencode.json').read_text())
        self.assertEqual(data['mcp'], {'kova': {'type': 'local', 'command': ['kova', '--mcp'], 'enabled': True}})

    def test_opencode_v1_schema(self):
        self.args.agents = ['opencode']
        with patch.object(setup, 'client_version', return_value=(1, 2, 27)):
            self.run_setup()
        data = json.loads((self.config / 'opencode/opencode.json').read_text())
        provider = data['provider']['kondash']
        self.assertEqual(provider['npm'], '@ai-sdk/openai-compatible')
        self.assertEqual(provider['options']['baseURL'], 'http://127.0.0.1:8317/v1')

    def test_pi_saved_auth_requires_replace_and_preserves_other_credentials(self):
        self.args.agents = ['pi']
        auth = self.write(self.home / '.pi/agent/auth.json',
                          '{"other":{"type":"api_key","key":"synthetic-other"},"kondash":{"type":"api_key","key":"synthetic-old"}}')
        with self.assertRaises(setup.SetupError) as caught:
            self.run_setup()
        self.assertIn('--replace', caught.exception.fix)
        self.args.replace = True
        self.run_setup()
        data = json.loads(auth.read_text())
        self.assertEqual(data['other']['key'], 'synthetic-other')
        self.assertTrue(data['kondash']['key'].startswith('!'))

    def test_nonobject_provider_section_is_not_clobbered(self):
        self.write(self.config / 'opencode/opencode.json', '{"providers":null}')
        with self.assertRaises(setup.SetupError):
            self.run_setup()
        self.assertFalse((self.gateway / 'agents').exists())

    def test_failed_write_rolls_back_prior_files(self):
        plan = setup.Plan(self.gateway / 'agents')
        existing = self.write(self.home / 'existing.json', '{}')
        plan.entry(existing, ('providers', 'kondash'), {})
        plan.file(self.home / 'new-file', 'synthetic')
        original_write = setup.atomic_write

        def fail_new(path, *args):
            if path.name == 'new-file':
                raise OSError('synthetic failure')
            return original_write(path, *args)

        with patch.object(setup, 'atomic_write', side_effect=fail_new), self.assertRaisesRegex(setup.SetupError, 'rolled back'):
            plan.apply()
        self.assertEqual(existing.read_text(), '{}')
        self.assertFalse((self.home / 'new-file').exists())

    def test_concurrent_edit_refused(self):
        plan = setup.Plan(self.gateway / 'agents')
        path = self.write(self.home / 'existing.json', '{}')
        plan.entry(path, ('providers', 'kondash'), {})
        path.write_text('{"concurrent":true}')
        with self.assertRaisesRegex(setup.SetupError, 'concurrent'):
            plan.apply()

    def test_checkout_destinations_refused(self):
        with self.assertRaisesRegex(setup.SetupError, 'outside this checkout'):
            setup.safe_path(setup.ROOT / 'must-not-create.json')

    def test_versions_with_v_prefix(self):
        # Avoid this test class's general mock of client_version.
        patch.stopall()
        with patch.object(setup.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='opencode v2.2.0\n')):
            self.assertEqual(setup.client_version('/fake/opencode'), (2, 2, 0))


class DiscoveryTests(unittest.TestCase):
    def test_gateway_url_restricted_before_reading_key(self):
        for url in ('https://example.test/v1', 'http://127.0.0.1/v1?key=x',
                    'http://key@localhost/v1', 'http://localhost/not-an-api'):
            with self.subTest(url=url), self.assertRaises(setup.SetupError):
                setup.gateway_models(url, Path('/does-not-exist'))

    def test_discovers_only_paired_routes(self):
        payload = io.StringIO(json.dumps({'data': [{'id': value} for value in
                               ['example-coder', 'example-coder-proxy', 'unrelated']]}))
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'key'
            key.write_text('synthetic-local')
            with patch.object(setup.urllib.request, 'build_opener') as opener:
                opener.return_value.open.return_value.__enter__.return_value = payload
                self.assertEqual(setup.gateway_models('http://127.0.0.1:8317/v1', key), ['example-coder'])
                request = opener.return_value.open.call_args.args[0]
                self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic-local')

    def test_http_error_withholds_secret_response(self):
        error = urllib.error.HTTPError('http://localhost/v1/models', 401, 'Unauthorized', {},
                                       io.BytesIO(b'synthetic-private-detail'))
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'key'
            key.write_text('synthetic-private-detail')
            with patch.object(setup.urllib.request, 'build_opener') as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(setup.SetupError) as caught:
                    setup.gateway_models('http://localhost/v1', key)
                self.assertIn('HTTP 401', str(caught.exception))
                self.assertNotIn('synthetic-private-detail', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
