#!/usr/bin/env python3
"""Install or uninstall a stock CLIProxyAPI user service configured for KONDash."""
import argparse
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import pwd
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from terminal import fail, headline, ok, problem as show_problem, skip, step, table, tidy

DEFAULT_BASE_URL = "https://kondash.konverto.eu/v1"
VERSION = "8.0.44"
RELEASES = {
    "x86_64": ("amd64", "72b59dc9b79c1f6860031705c9e8d8676dab7cd3caef81009838928c91ab2beb"),
    "aarch64": ("aarch64", "81697a2a14a1c3e2cb27bb338c94faded4855c5dc418e502ccf3fa99e917d4ac"),
}
SERVICE = "cliproxyapi.service"
CLEANUP = "cliproxyapi-log-cleanup"
UNITS = (SERVICE, CLEANUP + ".service", CLEANUP + ".timer")
OWNER = "kondash-cliproxyapi"
PROXY_SUFFIX = "-proxy"  # translated (Chat Completions) route; plain IDs are the direct route
ROOT = Path(__file__).resolve().parent


class SetupError(Exception):
    def __init__(self, message, fix=None):
        super().__init__(message)
        self.fix = fix


def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".install-", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data if isinstance(data, bytes) else data.encode())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def paths():
    home = Path.home()
    result = {
        "config": Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "cliproxyapi",
        "data": Path(os.environ.get("XDG_DATA_HOME", home / ".local/share")) / "cliproxyapi",
        "cache": Path(os.environ.get("XDG_CACHE_HOME", home / ".cache")) / "cliproxyapi",
        "units": Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "systemd/user",
        "bin": home / ".local/bin",
    }
    for name, value in result.items():
        result[name] = value.expanduser().resolve()
        if result[name] == ROOT or ROOT in result[name].parents:
            raise SetupError("Installation and credential directories must be outside this checkout.")
    return result


def validate_base_url(value):
    value = value.rstrip("/")
    url = urllib.parse.urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment):
        raise SetupError("The upstream API URL must use HTTPS and contain no credentials or query.")
    return value


def read_api_key(filename):
    if filename:
        try:
            value = Path(filename).expanduser().read_text().strip()
        except OSError:
            raise SetupError("Cannot open the supplied API-key file.",
                             "Check the path and that your user can read it.") from None
    else:
        if not sys.stdin.isatty():
            raise SetupError("No terminal to ask for the API key.",
                             "Put the key in a private file and pass --api-key-file /path/to/file.")
        value = getpass.getpass("KONDash API key (hidden): ").strip()
    if not value or any(character.isspace() for character in value):
        raise SetupError("The API key is empty or contains spaces.",
                         "Copy the key again from KONDash, without spaces or line breaks.")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


def discover_models(base_url, key):
    request = urllib.request.Request(base_url + "/models",
                                     headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise SetupError(f"KONDash rejected the request (HTTP {status}); response body withheld.",
                         "Check that the API key is correct and still active, then run with --reconfigure."
                         if status in (401, 403) else "KONDash may be unavailable; try again later.") from None
    except (OSError, ValueError):
        raise SetupError("Cannot reach KONDash at " + base_url,
                         "Check your internet connection, VPN or proxy settings (HTTPS_PROXY).") from None
    entries = result.get("data") if isinstance(result, dict) else None
    if not isinstance(entries, list):
        raise SetupError("The model endpoint did not return an OpenAI-compatible model list.")
    models = sorted({item["id"] for item in entries if isinstance(item, dict)
                     and isinstance(item.get("id"), str) and item["id"]
                     and not any(ord(c) < 32 for c in item["id"])})
    if not models:
        raise SetupError("No models are enabled for this API key.",
                         "Ask your KONDash administrator to enable models for your key.")
    if set(models) & {model + PROXY_SUFFIX for model in models}:
        raise SetupError("The upstream model IDs conflict with the reserved " + PROXY_SUFFIX + " alias suffix.")
    return models


def build_config(base_url, models, upstream_key, local_key, port, config_dir, management_key):
    return {
        "config-version": 8,
        "server": {"host": "127.0.0.1", "port": port},
        "management": {"secret-key": management_key, "allow-remote": False,
                       "disable-control-panel": False, "disable-auto-update-panel": True},
        "access": {"api-keys": [local_key]},
        "oauth": {"auth-dir": str(config_dir / "auth")},
        "multimedia": {"disable-image-generation": True},
        "api-keys": {
            "codex": [{"name": "kondash-responses", "base-url": base_url,
                       "keys": [{"api-key": upstream_key, "disable-codex-cloaking": True}],
                       "models": [{"name": model, "alias": model,
                                   "input-modalities": ["text"], "output-modalities": ["text"]}
                                  for model in models]}],
            "openai-compatibility": [{"name": "OpenAPI-Compatible",
                                      "base-url": f"http://127.0.0.1:{port}/v1",
                                      "keys": [{"api-key": local_key}],
                                      "models": [{"name": model, "alias": model + PROXY_SUFFIX,
                                                  "use-max-completion-tokens": True} for model in models]}],
        },
        "observability": {"logs": {"debug": False, "logging-to-file": True, "request-log": False,
                                    "error-logs-max-files": 5, "logs-max-total-size-mb": 100},
                          "usage": {"usage-statistics-enabled": False}},
    }


def yaml_dump(value, indent=0):
    """Emit the JSON-compatible YAML subset used by stock CLIProxyAPI, without dependencies."""
    prefix = " " * indent
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.extend([prefix + key + ":", yaml_dump(item, indent + 2)])
            else:
                lines.append(prefix + key + ": " + json.dumps(item, ensure_ascii=False))
        return "\n".join(lines)
    if isinstance(value, list):
        return "\n".join(prefix + "-\n" + yaml_dump(item, indent + 2)
                         if isinstance(item, (dict, list)) else prefix + "- " + json.dumps(item, ensure_ascii=False)
                         for item in value)
    raise SetupError("Unsupported configuration value.")


def fetch_release(url):
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            archive = response.read(200 * 1024 * 1024 + 1)
    except OSError:
        raise SetupError("Cannot download CLIProxyAPI from GitHub.",
                         "Check your internet connection or proxy (HTTPS_PROXY), or download\n"
                         + url + "\nyourself into ~/.cache/cliproxyapi/ and run the installer again.") from None
    if len(archive) > 200 * 1024 * 1024:
        raise SetupError("The release archive exceeds the download size limit.")
    return archive


def install_binary(targets, machine=None):
    machine = machine or platform.machine()
    if machine not in RELEASES:
        raise SetupError("Supported Linux architectures: x86_64 and aarch64.")
    arch, expected = RELEASES[machine]
    name = f"CLIProxyAPI_{VERSION}_linux_{arch}_no-plugin.tar.gz"
    archive_path = targets["cache"] / name
    if archive_path.exists():
        archive = archive_path.read_bytes()
        ok("Using cached release " + name)
    else:
        archive = fetch_release(f"https://github.com/router-for-me/CLIProxyAPI/releases/download/v{VERSION}/{name}")
        ok(f"Downloaded {name} from GitHub ({len(archive) / 1048576:.1f} MB)")
    if hashlib.sha256(archive).hexdigest() != expected:
        raise SetupError("Release checksum verification failed; no executable was installed.")
    ok("SHA-256 checksum verified")
    atomic_write(archive_path, archive)
    release_dir = targets["data"] / "releases" / VERSION
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
        for name, mode in (("cli-proxy-api", 0o755), ("LICENSE", 0o644)):
            matches = [entry for entry in package.getmembers() if Path(entry.name).name == name and entry.isfile()]
            if len(matches) != 1 or matches[0].size > 200 * 1024 * 1024:
                raise SetupError("The verified release archive has an unexpected layout.")
            atomic_write(release_dir / name, package.extractfile(matches[0]).read(), mode)
    ok("Installed to " + tidy(release_dir))
    return release_dir / "cli-proxy-api"


def replace_symlink(path, target):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".link-", dir=path.parent) as temporary:
        link = Path(temporary) / "link"
        link.symlink_to(target)
        os.replace(link, path)


def unit_quote(value):
    value = str(value)
    if any(c in value for c in ("\n", "\r", "\0")):
        raise SetupError("Installation paths cannot contain control characters.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def service_unit(targets):
    template = (ROOT / "systemd/cliproxyapi.service.in").read_text()
    directory = str(targets["config"])
    if any(c in directory for c in ("\n", "\r", "\0")):
        raise SetupError("Installation paths cannot contain control characters.")
    return (template.replace("@BINARY@", unit_quote(targets["data"] / "cli-proxy-api"))
            .replace("@CONFIG@", unit_quote(targets["config"] / "config.yaml"))
            .replace("@DIRECTORY@", directory.replace("%", "%%")))


def unit_files(targets):
    logs = unit_quote(targets["config"] / "auth" / "logs")
    return {SERVICE: service_unit(targets),
            **{CLEANUP + suffix: (ROOT / f"systemd/{CLEANUP}{suffix}.in").read_text().replace("@LOGS@", logs)
               for suffix in (".service", ".timer")}}


def command(arguments, description, fix=None):
    result = subprocess.run(arguments, capture_output=True, text=True)
    if result.returncode:
        raise SetupError(description + " failed.", fix or "See what went wrong: journalctl --user -u " + SERVICE + " -n 50")
    return result.stdout.strip()


def wait_ready(port, key):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models",
                                             headers={"Authorization": "Bearer " + key})
            with opener.open(request, timeout=1) as response:
                if isinstance(json.load(response).get("data"), list):
                    return
        except (OSError, ValueError, AttributeError):
            time.sleep(0.1)
    raise SetupError("CLIProxyAPI did not start answering within 20 seconds.", "See what went wrong: journalctl --user -u " + SERVICE + " -n 50")


def read_or_create_key(path):
    value = path.read_text().strip() if path.exists() else secrets.token_urlsafe(48)
    if not value or any(c.isspace() for c in value):
        raise SetupError(f"The local {path.name} file is invalid; contents withheld.")
    return value


def systemd_fix():
    if "microsoft" in platform.release().lower():
        return ("WSL: enable systemd by adding these lines to /etc/wsl.conf (sudo needed):\n"
                "  [boot]\n  systemd=true\n"
                "then run `wsl --shutdown` in Windows PowerShell and open WSL again")
    return ("Install systemd with your package manager, or use a distribution that runs it\n"
            "(containers and some minimal systems run without systemd)")


def user_manager_reachable():
    try:
        command(["systemctl", "--user", "show", "--property=Version"], "Connecting to the user-service manager")
        return True
    except SetupError:
        return False


def install(args):
    step("Checking your system")
    problems = []

    def problem(message, fix):
        problems.append(message)
        show_problem(message, fix)

    if sys.platform != "linux":
        raise SetupError("This installer supports Linux only.", "Run it on Linux, or inside WSL on Windows.")
    if os.getuid() == 0:
        raise SetupError("Do not run this installer as root.", "Run it again as your normal user, without sudo.")
    if platform.machine() not in RELEASES:
        problem(f"Unsupported CPU architecture: {platform.machine()}", "Use an x86_64 or aarch64 (ARM64) Linux machine.")
    missing = [tool for tool in ("systemctl", "loginctl") if not shutil.which(tool)]
    if missing:
        problem(" and ".join(missing) + " not found", systemd_fix())
    elif not user_manager_reachable():
        problem("Cannot reach your systemd user service manager",
                systemd_fix() if "microsoft" in platform.release().lower() else
                "Run the installer from a normal login session (desktop terminal or direct SSH login).\n"
                "After `su` or `sudo -u`, user services are not available; log in as the user instead.")
    else:
        ok("systemd user services are available")
    targets = paths()
    config_path = targets["config"] / "config.yaml"
    metadata_path = targets["config"] / "kondash-install.json"
    unit_path = targets["units"] / SERVICE
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    if config_path.exists() and metadata.get("managed_by") != OWNER:
        problem("An existing CLIProxyAPI configuration was not created by this installer: " + tidy(config_path),
                "Move it aside (e.g. rename it), then run the installer again. It was not changed.")
    for name in UNITS:
        if (targets["units"] / name).exists() and not (targets["units"] / name).read_text().startswith("# Managed by " + OWNER):
            problem(f"An existing {name} was not created by this installer",
                    f"Disable and move aside {tidy(targets['units'] / name)}, then run the installer again.")
    cli_path = targets["bin"] / "cli-proxy-api"
    if (cli_path.exists() or cli_path.is_symlink()) and (
            not cli_path.is_symlink() or cli_path.resolve() != (targets["data"] / "cli-proxy-api").resolve()):
        problem("Another cli-proxy-api command already exists: " + tidy(cli_path),
                "Remove or rename it, then run the installer again. It was not replaced.")
    port = args.port if args.port is not None else metadata.get("port", 8317)
    if not 1 <= port <= 65535:
        raise SetupError("Port must be between 1 and 65535.", "Pass a valid port, e.g. --port 8318.")
    active = not missing and subprocess.run(["systemctl", "--user", "is-active", "--quiet", SERVICE]).returncode == 0
    if active and not unit_path.exists():
        problem("A different cliproxyapi.service is already running",
                "Stop and disable it: systemctl --user disable --now " + SERVICE)
    elif active:
        ok("CLIProxyAPI is already running; it will be restarted")
    else:
        try:
            with socket.socket() as probe:
                # Like the Go server: ignore TIME_WAIT leftovers from a just-stopped service.
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", port))
            ok(f"Port {port} is free")
        except OSError:
            problem(f"Port {port} is already in use",
                    f"Find the program using it: ss -ltnp 'sport = :{port}'\n"
                    f"Stop it, or install on another port: python3 install.py --reconfigure --port {port + 1}")
    if problems:
        count = "1 problem" if len(problems) == 1 else f"{len(problems)} problems"
        raise SetupError(f"{count} must be fixed first; nothing was changed.", "Follow the steps above, then run the installer again.")
    reconfigure = not config_path.exists() or args.reconfigure
    if args.api_key_file and not reconfigure:
        raise SetupError("--api-key-file only applies to a new configuration.",
                         "Add --reconfigure to replace the existing configuration with the new key.")
    if args.port and metadata.get("port", args.port) != args.port and not reconfigure:
        raise SetupError("Changing the configured port requires --reconfigure.",
                         f"Run: python3 install.py --reconfigure --port {args.port}")
    if reconfigure and config_path.exists():
        changed = hashlib.sha256(config_path.read_bytes()).hexdigest() != metadata.get("config_sha256")
        if changed and not args.replace_config:
            raise SetupError("The configuration has local edits: " + tidy(config_path),
                             "Add --replace-config to replace it; the current file is backed up first.")
    key_path = targets["config"] / "proxy-key"
    management_path = targets["config"] / "management-key"
    new_keys = {path: not path.exists() for path in (key_path, management_path)}
    local_key = read_or_create_key(key_path)
    management_key = read_or_create_key(management_path)
    config = None
    if reconfigure:
        base_url = validate_base_url(args.base_url)
        step("Connecting to KONDash at " + base_url)
        upstream_key = read_api_key(args.api_key_file)
        models = discover_models(base_url, upstream_key)
        config = yaml_dump(build_config(base_url, models, upstream_key, local_key, port, targets["config"], management_key)) + "\n"
        ok("API key accepted")
        ok(f"{len(models)} models available (IDs are kept in the local configuration only)")
    else:
        step("Using the existing configuration")
        skip("Keeping " + tidy(config_path) + "; use --reconfigure to change the API key or refresh models")
    step(f"Installing CLIProxyAPI {VERSION}")
    for name in ("config", "data", "cache"):
        targets[name].mkdir(parents=True, exist_ok=True)
        targets[name].chmod(0o700)
    binary = install_binary(targets)
    replace_symlink(targets["data"] / "cli-proxy-api", binary)
    replace_symlink(cli_path, targets["data"] / "cli-proxy-api")
    ok("Command available as " + tidy(cli_path))
    step("Writing configuration")
    atomic_write(key_path, local_key + "\n")
    atomic_write(management_path, management_key + "\n")
    for path, label in ((key_path, "local API key"), (management_path, "web UI password")):
        ok(("Created " if new_keys[path] else "Kept existing ") + label + " in " + tidy(path))
    if config is not None:
        backup = None
        if config_path.exists():
            backup = config_path.with_name("config.yaml.backup-" + time.strftime("%Y%m%d-%H%M%S"))
            atomic_write(backup, config_path.read_bytes())
        atomic_write(config_path, config)
        ok("Saved " + tidy(config_path) + (" (previous version: " + backup.name + ")" if backup else ""))
        metadata.update(config_sha256=hashlib.sha256(config.encode()).hexdigest(), port=port)
    metadata.update(managed_by=OWNER, version=VERSION)
    atomic_write(metadata_path, json.dumps(metadata, indent=2) + "\n")
    step("Starting the service")
    for name, unit in unit_files(targets).items():
        atomic_write(targets["units"] / name, unit, 0o644)
    command(["systemctl", "--user", "daemon-reload"], "Reloading user units")
    ok("Installed systemd user units in " + tidy(targets["units"]))
    command(["systemctl", "--user", "enable", SERVICE], "Enabling autostart")
    command(["systemctl", "--user", "restart", SERVICE], "Starting CLIProxyAPI")
    command(["systemctl", "--user", "enable", "--now", CLEANUP + ".timer"], "Enabling daily log cleanup")
    ok("Daily log cleanup scheduled (keeps 30 days)")
    wait_ready(port, local_key)
    ok(f"Service started and answering on port {port}")
    if config is not None:
        # CLIProxyAPI replaces the plaintext management secret with a bcrypt hash on startup.
        metadata.update(config_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest())
        atomic_write(metadata_path, json.dumps(metadata, indent=2) + "\n")
    username = pwd.getpwuid(os.getuid()).pw_name
    command(["loginctl", "--no-ask-password", "enable-linger", username], "Enabling startup before login",
            f"Your system does not allow this by default. Ask an administrator to run:\n  sudo loginctl enable-linger {username}\n"
            "Everything else is installed; without this, the service only runs while you are logged in.")
    ok("Autostart enabled, also at boot before you log in")
    headline(f"CLIProxyAPI {VERSION} is ready.")
    print()
    table([("Configuration", tidy(config_path)),
           ("Logs", tidy(targets["config"] / "auth" / "logs") + " (kept 30 days)"),
           ("Service", "systemctl --user status " + SERVICE),
           None,
           ("API endpoint", f"http://127.0.0.1:{port}/v1"),
           ("API key file", tidy(key_path)),
           None,
           ("Web UI", f"http://127.0.0.1:{port}/management.html (only from this machine)"),
           ("Web UI password", management_key)])
    print()
    print("Next: run python3 configure_agents.py to connect your coding agents.")


def uninstall(purge=False):
    if sys.platform != "linux" or os.getuid() == 0:
        raise SetupError("Do not run this as root.", "Run it again as your normal user, without sudo.")
    targets = paths()
    unit_paths = [targets["units"] / name for name in UNITS]
    cli_path = targets["bin"] / "cli-proxy-api"
    metadata_path = targets["config"] / "kondash-install.json"
    present = [targets[name] for name in ("config", "data", "cache")] + unit_paths + [cli_path]
    if not any(path.exists() or path.is_symlink() for path in present):
        skip("CLIProxyAPI is already uninstalled.")
        return
    try:
        metadata = json.loads(metadata_path.read_text())
    except (OSError, ValueError):
        raise SetupError("Cannot verify installer ownership; no files were removed.") from None
    if not isinstance(metadata, dict) or metadata.get("managed_by") != OWNER:
        raise SetupError("An unmanaged CLIProxyAPI installation was found; no files were removed.")
    if any(path.exists() and not path.read_text().startswith("# Managed by " + OWNER) for path in unit_paths):
        raise SetupError("A service unit is unmanaged; no files were removed.")
    if (cli_path.exists() or cli_path.is_symlink()) and (
            not cli_path.is_symlink() or cli_path.resolve() != (targets["data"] / "cli-proxy-api").resolve()):
        raise SetupError("The cli-proxy-api command is unmanaged; no files were removed.")
    # Validate all ownership markers before stopping services or deleting anything.
    step("Uninstalling CLIProxyAPI")
    installed = [path for path in unit_paths if path.exists()]
    if installed:
        command(["systemctl", "--user", "disable", "--now"] + [path.name for path in installed],
                "Stopping and disabling CLIProxyAPI")
        for path in installed:
            path.unlink()
        command(["systemctl", "--user", "daemon-reload"], "Reloading user units")
        ok("Stopped and removed " + ", ".join(path.name for path in installed))
    if cli_path.is_symlink():
        cli_path.unlink()
        ok("Removed command " + tidy(cli_path))
    for name in ("data", "cache"):
        if targets[name].exists():
            shutil.rmtree(targets[name])
    ok("Removed installed releases and download cache")
    if purge and targets["config"].exists():
        shutil.rmtree(targets["config"])
        ok("Removed configuration, keys, logs and backups in " + tidy(targets["config"]))
    elif not purge:
        skip("Kept configuration and keys in " + tidy(targets["config"]) + " (use --uninstall --purge to remove them)")
    skip("Your original API-key file and the lingering setting were not changed")
    headline("CLIProxyAPI uninstalled.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uninstall", action="store_true", help="Stop and remove this managed installation; preserve private configuration")
    parser.add_argument("--purge", action="store_true", help="With --uninstall, also remove local configuration and credentials")
    parser.add_argument("--api-key-file", type=Path, help="Read the upstream token from a private file instead of prompting")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Override the preconfigured HTTPS KONDash API URL")
    parser.add_argument("--port", type=int, help="Local loopback port (default: 8317)")
    parser.add_argument("--reconfigure", action="store_true", help="Set the API key again and refresh the discovered models")
    parser.add_argument("--replace-config", action="store_true", help="Allow reconfiguration to replace locally edited config, with backup")
    args = parser.parse_args()
    if args.purge and not args.uninstall:
        parser.error("--purge requires --uninstall")
    if args.uninstall and (args.api_key_file or args.reconfigure or args.replace_config
                           or args.port is not None or args.base_url != DEFAULT_BASE_URL):
        parser.error("--uninstall cannot be combined with installation/reconfiguration options")
    try:
        if args.uninstall:
            uninstall(purge=args.purge)
        else:
            install(args)
    except (SetupError, OSError, ValueError, tarfile.TarError) as error:
        print()
        if isinstance(error, SetupError):
            fail("Setup failed: " + str(error), error.fix)
        else:
            fail("Setup failed: a local file or archive operation failed; credentials were not logged.",
                 "Check free disk space and that your home directory is writable, then run the installer again.")
        return 1
    except (KeyboardInterrupt, EOFError):
        print()
        fail("Setup cancelled.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
