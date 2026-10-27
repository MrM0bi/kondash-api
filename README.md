# KONDash for CLIProxyAPI

Install a normal [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) instance on Linux with KONDash already configured. **The only required input is your KONDash API key.**

```bash
python3 install.py
```

The installer prompts without echoing the key, discovers the models your key can access, downloads a checksum-pinned stock CLIProxyAPI binary, and starts an authenticated localhost service. It enables user-service autostart and lingering so the gateway can run before login and after logout.

Requirements: Linux with a working systemd user-service manager, Python 3.9+, and x86_64 or aarch64. Run as your normal user, without `sudo`. Internet access is needed for KONDash model discovery and the initial GitHub release download.

The preconfigured API URL is `https://kondash.konverto.eu/v1`. Model IDs are discovered after authentication and kept in the private local configuration. No organization-specific model catalog is shipped in this repository.

## What gets installed

- The official CLIProxyAPI **8.0.44** binary, verified against a pinned SHA-256 digest. There is no proxy fork or custom server process.
- An ordinary CLIProxyAPI YAML configuration at `~/.config/cliproxyapi/config.yaml`, readable only by your user.
- The systemd user unit `cliproxyapi.service`, which executes the stock binary directly.
- An automatically generated local client credential in `~/.config/cliproxyapi/proxy-key`.
- An automatically generated web UI password in `~/.config/cliproxyapi/management-key`.

The service listens at **`http://127.0.0.1:8317/v1`**. The management web UI is at `http://127.0.0.1:8317/management.html` (localhost only, including from Windows when running in WSL); its password is in `~/.config/cliproxyapi/management-key`. Automatic image-tool injection is disabled. The installation does not depend on this checkout after setup; you may move or remove the clone.

```bash
systemctl --user status cliproxyapi.service
systemctl --user restart cliproxyapi.service
systemctl --user stop cliproxyapi.service
```

## Use an existing private key file

```bash
python3 install.py --api-key-file /path/to/private/kondash-token
```

The key itself is never accepted as a command-line argument or printed. Both the upstream token and generated gateway configuration stay outside the repository. Coding agents authenticate with the separate local proxy key.

## Add the gateway to coding agents

After installing and starting the gateway:

```bash
python3 configure_agents.py --dry-run
python3 configure_agents.py
```

This discovers local models and configures installed **OpenCode, Pi, Codex, and Claude Code**. It merges OpenCode/Pi providers and creates separate Codex/Claude profiles, preserving existing defaults. Credentials stay in the private gateway key file. Existing files are backed up before changes; conflicting or edited KONDash entries require `--replace`.

To configure only selected clients, use `python3 configure_agents.py --agents opencode pi`. See [client setup](docs/clients.md) for activation commands, supported versions, and Codex model metadata requirements.

The provider appears as **KONDash**. Pi/OpenCode thinking controls are enabled for every model by default; limit or disable them per model with `--model '<deployment-id>' --reasoning-efforts [levels]`. [Live compatibility checks](docs/clients.md#compatibility-checks) cover coding tools, MCP, and effort forwarding using `test_agents.py`.

## Reconfigure or refresh available models

```bash
python3 install.py --reconfigure
```

This asks for the upstream key again and refreshes the model list. Existing configuration is backed up privately. If you edited the generated YAML, an additional `--replace-config` is required before replacing it.

Running `python3 install.py` again without reconfiguration preserves your YAML and local client key. A different upstream URL or local port can be supplied using `--base-url` and `--port` during initial setup or reconfiguration. Standard XDG config/data/cache directories are supported.

## Uninstall

To uninstall while keeping private configuration:

```bash
python3 install.py --uninstall
```

For a clean reset before a fresh install, use `python3 install.py --uninstall --purge`. This also deletes the local configuration, generated client key, auth files, and configuration backups. Your original external API-key file is not removed.

## Documentation

- [Installation, files, and service management](docs/setup.md)
- [KONDash API behavior and compatibility](docs/kondash.md)
- [Using coding-agent clients](docs/clients.md)
- [Keeping private information out of the repository](docs/privacy.md)
- [Sanitized CLIProxyAPI configuration example](examples/config.example.yaml)

The gateway installer and optional client setup are separate commands. Client configuration and feature support depend on the client version and the selected upstream model.

## Development

Both scripts use Python's standard library only. Tests use synthetic credentials, synthetic model names, and temporary directories; they make no live API calls.

The separate `test_agents.py` runner is an opt-in integration check that makes real gateway requests. Its reports and agent configurations stay in private temporary directories outside the checkout.

```bash
python3 -m unittest discover -s tests -v
```
