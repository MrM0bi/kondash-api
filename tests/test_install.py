"""Installer tests use synthetic credentials and model IDs; no live API access."""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import stat
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

import install


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="installer-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.targets = {name: self.root / name for name in ("config", "data", "cache", "units", "bin")}
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))  # keep progress lines out of test output

    def test_private_atomic_files(self):
        path = self.root / "private/key"
        install.atomic_write(path, "synthetic-secret")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        install.atomic_write(path, "replacement")
        self.assertEqual(path.read_text(), "replacement")
        self.assertEqual(list(path.parent.glob(".install-*")), [])

    def test_discovery_deduplicates_models_without_catalog_assumptions(self):
        payload = io.StringIO(json.dumps({"data": [{"id": "example-coder"}, {"id": "example-coder"},
                                                   {"id": "vendor/example-fast"}]}))
        with patch.object(install.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value = payload
            self.assertEqual(install.discover_models("https://gateway.example/v1", "synthetic-key"),
                             ["example-coder", "vendor/example-fast"])

    def test_discovery_errors_do_not_expose_response_or_token(self):
        error = urllib.error.HTTPError("https://gateway.example/v1/models", 401, "Unauthorized", {},
                                       io.BytesIO(b"echoed synthetic-private-value"))
        with patch.object(install.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(install.SetupError) as caught:
                install.discover_models("https://gateway.example/v1", "synthetic-private-value")
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertNotIn("synthetic-private-value", str(caught.exception))

    def test_rejects_model_alias_collisions(self):
        payload = io.StringIO(json.dumps({"data": [{"id": "example"}, {"id": "example-proxy"}]}))
        with patch.object(install.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value = payload
            with self.assertRaisesRegex(install.SetupError, "conflict"):
                install.discover_models("https://gateway.example/v1", "synthetic-key")

    def test_api_tokens_are_not_forwarded_on_redirect(self):
        self.assertIsNone(install.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))

    def test_https_endpoint_required(self):
        for value in ("http://gateway.example/v1", "https://token@gateway.example/v1", "https://gateway.example/?key=value"):
            with self.subTest(value=value), self.assertRaises(install.SetupError):
                install.validate_base_url(value)

    def test_compatibility_route_keeps_credentials_separate(self):
        config = install.build_config("https://gateway.example/v1", ["example-coder"],
                                      "synthetic-upstream", "synthetic-local", 8456, self.root,
                                      "synthetic-management")
        self.assertEqual(config["server"], {"host": "127.0.0.1", "port": 8456})
        upstream = config["api-keys"]["codex"][0]
        bridge = config["api-keys"]["openai-compatibility"][0]
        self.assertEqual(bridge["base-url"], "http://127.0.0.1:8456/v1")
        self.assertEqual(bridge["keys"][0]["api-key"], "synthetic-local")
        self.assertEqual(upstream["keys"][0]["api-key"], "synthetic-upstream")
        self.assertEqual(bridge["models"][0]["name"], upstream["models"][0]["alias"])
        self.assertNotEqual(bridge["models"][0]["alias"], upstream["models"][0]["alias"])
        self.assertTrue(config["multimedia"]["disable-image-generation"])
        self.assertEqual(config["management"]["secret-key"], "synthetic-management")
        self.assertFalse(config["management"]["allow-remote"])
        self.assertFalse(config["management"]["disable-control-panel"])

    def test_yaml_writer_quotes_secret_like_scalars(self):
        rendered = install.yaml_dump({"keys": ["a: b # c\nquoted \"value\""], "enabled": False})
        self.assertIn(json.dumps('a: b # c\nquoted "value"'), rendered)
        self.assertIn("enabled: false", rendered)

    def test_reliability_defaults_do_not_override_inference_controls(self):
        profile = install.reliability_profile()
        self.assertEqual(profile["routing"]["cooldown"]["transient-error-cooldown-seconds"], -1)
        self.assertEqual(profile["routing"]["retry"]["request-retry"], 1)
        self.assertTrue(profile["requests"]["streaming"]["send-keepalive-before-ft"])
        self.assertNotIn("payload", profile["requests"])

    def test_workaround_merge_retains_custom_payload_rules_and_is_idempotent(self):
        current = install.build_config("https://gateway.example/v1", ["example-coder"],
                                       "synthetic-upstream", "synthetic-local", 8317, self.root,
                                       "synthetic-management")
        custom = {"models": [{"name": "example-coder", "protocol": "codex"}],
                  "params": {"max_output_tokens": 4096, "temperature": 0.7}}
        current["requests"]["payload"] = {"default": [custom]}
        original = json.loads(json.dumps(current))
        patch = install.workaround_patch(current)
        self.assertNotIn("payload", patch["requests"])
        self.assertTrue(install.contains_patch(current, patch))
        self.assertEqual(current, original)
        with self.assertRaisesRegex(install.SetupError, "provider is missing"):
            install.workaround_patch({})

    def test_workaround_migrates_unsupported_legacy_model_fields_only(self):
        current = install.build_config("https://gateway.example/v1", ["example-coder"],
                                       "synthetic-upstream", "synthetic-local", 8317, self.root,
                                       "synthetic-management")
        old = json.loads(json.dumps(current))
        model = old["api-keys"]["codex"][0]["models"][0]
        model.update({"input-modalities": ["text"], "output-modalities": ["text"]})
        patch = install.workaround_patch(old)
        self.assertEqual(patch["api-keys"]["codex"], current["api-keys"]["codex"])
        self.assertIn("input-modalities", old["api-keys"]["codex"][0]["models"][0])
        self.assertNotIn("api-keys", install.workaround_patch(current))

    def workaround_context(self, edited=False):
        current = install.build_config("https://gateway.example/v1", ["example-coder"],
                                       "synthetic-upstream", "synthetic-local", 8317, self.root,
                                       "synthetic-management")
        del current["routing"]
        del current["requests"]
        current["custom-section"] = {"keep": "user-setting"}
        config = self.targets["config"] / "config.yaml"
        install.atomic_write(config, json.dumps(current))
        metadata = {"managed_by": install.OWNER, "port": 8317,
                    "config_sha256": "pre-edit-hash" if edited else hashlib.sha256(config.read_bytes()).hexdigest()}
        install.atomic_write(self.targets["config"] / "kondash-install.json", json.dumps(metadata))
        install.atomic_write(self.targets["config"] / "management-key", "synthetic-management\n")
        install.atomic_write(self.targets["config"] / "proxy-key", "synthetic-local\n")
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(install, "paths", return_value=self.targets))
        stack.enter_context(patch.object(install.os, "getuid", return_value=12345))
        ready = stack.enter_context(patch.object(install, "wait_ready"))

        def management(port, key, method="GET", patch=None, etag=None):
            self.assertEqual((port, key), (8317, "synthetic-management"))
            data = json.loads(config.read_text())
            tag = '"' + hashlib.sha256(config.read_bytes()).hexdigest() + '"'
            if method == "PATCH":
                self.assertEqual(etag, tag)

                def merge(dst, source):
                    for k, v in source.items():
                        if isinstance(v, dict):
                            merge(dst.setdefault(k, {}), v)
                        else:
                            dst[k] = v
                merge(data, patch)
                install.atomic_write(config, json.dumps(data))
                return {"status": "ok"}, None
            return data, tag

        api = stack.enter_context(patch.object(install, "management_request", side_effect=management))
        return stack, api, ready, config

    def test_apply_workarounds_preserves_credentials_customizations_and_backup(self):
        stack, api, ready, config = self.workaround_context()
        original = config.read_bytes()
        before = json.loads(original)
        with stack:
            install.apply_workarounds()
            after = json.loads(config.read_bytes())
            self.assertEqual(after["api-keys"], before["api-keys"])
            self.assertEqual(after["custom-section"], before["custom-section"])
            self.assertEqual(after["management"], before["management"])
            self.assertEqual(after["server"], before["server"])
            backups = list(config.parent.glob("config.yaml.backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
            meta = json.loads((config.parent / "kondash-install.json").read_text())
            self.assertEqual(meta["config_sha256"], hashlib.sha256(config.read_bytes()).hexdigest())
            ready.assert_called_once_with(8317, "synthetic-local")
            saved = config.read_bytes()
            install.apply_workarounds()
            self.assertEqual(config.read_bytes(), saved)
            self.assertEqual(len(list(config.parent.glob("config.yaml.backup-*"))), 1)

    def test_apply_workarounds_keeps_local_edit_protection(self):
        stack, api, ready, config = self.workaround_context(edited=True)
        with stack:
            install.apply_workarounds()
        meta = json.loads((config.parent / "kondash-install.json").read_text())
        self.assertEqual(meta["config_sha256"], "pre-edit-hash")

    def test_apply_workarounds_refuses_mismatched_running_configuration(self):
        stack, api, ready, config = self.workaround_context()
        original = config.read_bytes()
        with stack:
            api.side_effect = None
            api.return_value = (json.loads(original), '"another-config"')
            with self.assertRaisesRegex(install.SetupError, "snapshots do not match"):
                install.apply_workarounds()
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(list(config.parent.glob("config.yaml.backup-*")), [])
            ready.assert_not_called()

    def test_apply_workarounds_failed_patch_keeps_metadata_and_private_backup(self):
        stack, api, ready, config = self.workaround_context()
        original = config.read_bytes()
        tag = '"' + hashlib.sha256(original).hexdigest() + '"'
        metadata = (config.parent / "kondash-install.json").read_bytes()
        with stack:
            api.side_effect = [(json.loads(original), tag), install.SetupError("precondition failed")]
            with self.assertRaisesRegex(install.SetupError, "precondition failed"):
                install.apply_workarounds()
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual((config.parent / "kondash-install.json").read_bytes(), metadata)
        self.assertEqual(list(config.parent.glob("config.yaml.backup-*"))[0].read_bytes(), original)

    def test_management_precondition_error_hides_response_contents(self):
        error = urllib.error.HTTPError("http://127.0.0.1:8317/v8/management/config", 412, "Conflict", {},
                                       io.BytesIO(b"synthetic-private-value"))
        with patch.object(install.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(install.SetupError, "not overwritten") as caught:
                install.management_request(8317, "synthetic-private-value", "PATCH", {}, '"old"')
        self.assertNotIn("synthetic-private-value", str(caught.exception))

    def test_workaround_mode_does_not_run_install_or_key_discovery(self):
        with patch.object(install.sys, "argv", ["install.py", "--apply-workarounds"]), \
                patch.object(install, "apply_workarounds") as apply, \
                patch.object(install, "install") as full_install:
            self.assertEqual(install.main(), 0)
        apply.assert_called_once_with()
        full_install.assert_not_called()
        for extra in (["--reconfigure"], ["--uninstall"], ["--port", "8318"]):
            with self.subTest(extra=extra), \
                    patch.object(install.sys, "argv", ["install.py", "--apply-workarounds"] + extra), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                install.main()

    def test_workarounds_refuse_unmanaged_installation_before_contacting_service(self):
        stack, api, ready, config = self.workaround_context()
        install.atomic_write(config.parent / "kondash-install.json", json.dumps({"managed_by": "someone-else"}))
        original = config.read_bytes()
        with stack, self.assertRaisesRegex(install.SetupError, "not installer-managed"):
            install.apply_workarounds()
        api.assert_not_called()
        self.assertEqual(config.read_bytes(), original)

    def archive(self):
        content = io.BytesIO()
        with tarfile.open(fileobj=content, mode="w:gz") as archive:
            for name, data in (("cli-proxy-api", b"synthetic-binary"), ("LICENSE", b"synthetic-license"),
                               ("../../must-not-extract", b"unwanted")):
                item = tarfile.TarInfo(name)
                item.size = len(data)
                archive.addfile(item, io.BytesIO(data))
        return content.getvalue()

    def test_release_checksum_failure_installs_no_executable(self):
        with patch.object(install, "RELEASES", {"test": ("test", "0" * 64)}), \
                patch.object(install, "fetch_release", return_value=self.archive()):
            with self.assertRaisesRegex(install.SetupError, "checksum"):
                install.install_binary(self.targets, "test")
        self.assertFalse((self.targets["data"] / "releases").exists())

    def test_archive_paths_are_not_extracted(self):
        archive = self.archive()
        with patch.object(install, "RELEASES", {"test": ("test", hashlib.sha256(archive).hexdigest())}), \
                patch.object(install, "fetch_release", return_value=archive):
            binary = install.install_binary(self.targets, "test")
        self.assertEqual(binary.read_bytes(), b"synthetic-binary")
        self.assertEqual(stat.S_IMODE(binary.stat().st_mode), 0o755)
        self.assertFalse((self.targets["data"] / "must-not-extract").exists())
        self.assertEqual(sorted(p.name for p in binary.parent.iterdir()), ["LICENSE", "cli-proxy-api"])

    def test_unit_has_no_checkout_or_credential_dependency(self):
        unit = install.service_unit(self.targets)
        self.assertIn("ExecStart=:", unit)
        self.assertIn("-config", unit)
        self.assertNotIn(str(install.ROOT), unit)
        self.assertNotIn("LoadCredential", unit)
        self.assertNotIn("python", unit)
        self.assertIn("WorkingDirectory=" + str(self.targets["config"]) + "\n", unit)
        self.assertIn('"/a path/100%%/file"', install.unit_quote("/a path/100%/file"))
        cleanup = install.unit_files(self.targets)[install.CLEANUP + ".service"]
        self.assertIn('find "' + str(self.targets["config"] / "auth/logs") + '" ', cleanup)
        self.assertIn("! -name main.log -mtime +30 -delete", cleanup)

    def managed_files(self):
        install.atomic_write(self.targets["config"] / "kondash-install.json",
                             json.dumps({"managed_by": install.OWNER}))
        install.atomic_write(self.targets["config"] / "proxy-key", "synthetic-local-key")
        install.atomic_write(self.targets["config"] / "config.yaml", "synthetic-private-config")
        binary = self.targets["data"] / "releases/test/cli-proxy-api"
        install.atomic_write(binary, "synthetic-binary", 0o755)
        install.replace_symlink(self.targets["data"] / "cli-proxy-api", binary)
        install.replace_symlink(self.targets["bin"] / "cli-proxy-api", self.targets["data"] / "cli-proxy-api")
        install.atomic_write(self.targets["cache"] / "archive", "synthetic-cache")
        for name, unit in install.unit_files(self.targets).items():
            install.atomic_write(self.targets["units"] / name, unit)

    def uninstall_context(self):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(install, "paths", return_value=self.targets))
        stack.enter_context(patch.object(install.os, "getuid", return_value=12345))
        commands = stack.enter_context(patch.object(install, "command"))
        output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        return stack, commands, output

    def test_uninstall_preserves_private_configuration(self):
        self.managed_files()
        stack, commands, output = self.uninstall_context()
        with stack:
            install.uninstall()
        self.assertTrue((self.targets["config"] / "proxy-key").exists())
        for name in ("data", "cache"):
            self.assertFalse(self.targets[name].exists())
        self.assertFalse((self.targets["units"] / install.SERVICE).exists())
        self.assertFalse((self.targets["bin"] / "cli-proxy-api").is_symlink())
        self.assertEqual(commands.call_args_list[0].args[0],
                         ["systemctl", "--user", "disable", "--now", *install.UNITS])
        self.assertFalse(any((self.targets["units"] / name).exists() for name in install.UNITS))
        self.assertNotIn("synthetic-private-config", output.getvalue())
        self.assertNotIn("synthetic-local-key", output.getvalue())

    def test_purge_removes_managed_config_but_preserves_external_key(self):
        self.managed_files()
        external_key = self.root / "original-api-key"
        external_key.write_text("synthetic-external-key")
        stack, commands, output = self.uninstall_context()
        with stack:
            install.uninstall(purge=True)
            install.uninstall(purge=True)
        self.assertFalse(self.targets["config"].exists())
        self.assertEqual(external_key.read_text(), "synthetic-external-key")
        self.assertEqual(commands.call_count, 2)
        self.assertIn("already uninstalled", output.getvalue())

    def test_purge_after_default_uninstall(self):
        self.managed_files()
        stack, commands, output = self.uninstall_context()
        with stack:
            install.uninstall()
            install.uninstall(purge=True)
        self.assertFalse(self.targets["config"].exists())
        self.assertEqual(commands.call_count, 2)

    def test_uninstall_refuses_unmanaged_unit_before_stopping_service(self):
        self.managed_files()
        (self.targets["units"] / install.SERVICE).write_text("Unrelated service")
        stack, commands, output = self.uninstall_context()
        with stack, self.assertRaisesRegex(install.SetupError, "unit is unmanaged"):
            install.uninstall(purge=True)
        commands.assert_not_called()
        self.assertTrue(self.targets["data"].exists())
        self.assertTrue(self.targets["config"].exists())

    def test_uninstall_refuses_unmanaged_command(self):
        self.managed_files()
        cli = self.targets["bin"] / "cli-proxy-api"
        cli.unlink()
        cli.write_text("Unrelated executable")
        stack, commands, output = self.uninstall_context()
        with stack, self.assertRaisesRegex(install.SetupError, "command is unmanaged"):
            install.uninstall(purge=True)
        commands.assert_not_called()
        self.assertEqual(cli.read_text(), "Unrelated executable")

    def test_uninstall_refuses_missing_ownership_metadata(self):
        self.managed_files()
        (self.targets["config"] / "kondash-install.json").unlink()
        stack, commands, output = self.uninstall_context()
        with stack, self.assertRaisesRegex(install.SetupError, "Cannot verify"):
            install.uninstall(purge=True)
        commands.assert_not_called()
        self.assertTrue(self.targets["data"].exists())

    def test_purge_requires_uninstall_mode(self):
        with patch.object(install.sys, "argv", ["install.py", "--purge"]), \
                contextlib.redirect_stderr(io.StringIO()), patch.object(install, "uninstall") as remove:
            with self.assertRaises(SystemExit) as caught:
                install.main()
        self.assertEqual(caught.exception.code, 2)
        remove.assert_not_called()

    def test_preflight_lists_every_problem_with_a_fix_before_changing_anything(self):
        install.atomic_write(self.targets["config"] / "config.yaml", "someone else's config")
        args = argparse.Namespace(api_key_file=None, base_url=install.DEFAULT_BASE_URL, port=None,
                                  reconfigure=False, replace_config=False)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(install, "paths", return_value=self.targets))
            stack.enter_context(patch.object(install.os, "getuid", return_value=12345))
            stack.enter_context(patch.object(install.shutil, "which", return_value="/usr/bin/example"))
            commands = stack.enter_context(patch.object(install, "command", return_value=""))
            stack.enter_context(patch.object(install.subprocess, "run", return_value=SimpleNamespace(returncode=1)))
            stack.enter_context(patch.object(install.socket, "socket")).return_value.__enter__.return_value.bind.side_effect = OSError
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            with self.assertRaisesRegex(install.SetupError, "2 problems") as caught:
                install.install(args)
        self.assertIn("not created by this installer", output.getvalue())
        self.assertIn("Port 8317 is already in use", output.getvalue())
        self.assertIn("ss -ltnp", output.getvalue())
        self.assertIn("run the installer again", caught.exception.fix)
        self.assertEqual(commands.call_count, 1)  # only the read-only user-manager probe
        self.assertEqual((self.targets["config"] / "config.yaml").read_text(), "someone else's config")
        self.assertFalse(self.targets["data"].exists())

    def test_reinstall_preserves_custom_configuration_and_local_key(self):
        args = argparse.Namespace(api_key_file=None, base_url="https://gateway.example/v1", port=8456,
                                  reconfigure=False, replace_config=False)

        def fake_binary(targets):
            binary = targets["data"] / "releases" / "test" / "cli-proxy-api"
            install.atomic_write(binary, "synthetic-binary", 0o755)
            return binary

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(install, "paths", return_value=self.targets))
            stack.enter_context(patch.object(install.os, "getuid", return_value=12345))
            stack.enter_context(patch.object(install.pwd, "getpwuid", return_value=SimpleNamespace(pw_name="example-user")))
            stack.enter_context(patch.object(install.shutil, "which", return_value="/usr/bin/example"))
            stack.enter_context(patch.object(install, "command", return_value=""))
            stack.enter_context(patch.object(install.subprocess, "run", return_value=SimpleNamespace(returncode=1)))
            stack.enter_context(patch.object(install.socket, "socket"))
            stack.enter_context(patch.object(install, "install_binary", side_effect=fake_binary))
            key_reader = stack.enter_context(patch.object(install, "read_api_key", return_value="synthetic-upstream"))
            stack.enter_context(patch.object(install, "discover_models", return_value=["example-coder"]))
            config = self.targets["config"] / "config.yaml"

            def server_hashes_secret(port, key):
                config.write_text(config.read_text().replace(
                    (self.targets["config"] / "management-key").read_text().strip(), "$2a$10$synthetic-hash"))
            stack.enter_context(patch.object(install, "wait_ready", side_effect=server_hashes_secret))
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            install.install(args)
            self.assertIn("$2a$10$synthetic-hash", config.read_text())
            args.reconfigure = True
            install.install(args)  # startup hashing is not a local edit
            args.reconfigure = False
            local_key = (self.targets["config"] / "proxy-key").read_bytes()
            config.write_text(config.read_text() + "# local customization\n")
            customized = config.read_bytes()
            install.install(args)
            self.assertEqual(config.read_bytes(), customized)
            self.assertEqual((self.targets["config"] / "proxy-key").read_bytes(), local_key)
            self.assertEqual(key_reader.call_count, 2)
            self.assertNotIn("synthetic-upstream", output.getvalue())
            self.assertIn((self.targets["config"] / "management-key").read_text().strip(), output.getvalue())
            self.assertNotIn("example-coder", output.getvalue())
            args.reconfigure = True
            with self.assertRaisesRegex(install.SetupError, "local edits"):
                install.install(args)


if __name__ == "__main__":
    unittest.main()
