# Installation and service management

## Fresh installation

Run the installer from this checkout as your normal user:

```bash
python3 install.py
```

Provide your KONDash token at the hidden prompt. For noninteractive setup, use `--api-key-file /path/to/private/token`; do not put the token itself in shell history.

The installer authenticates against the preconfigured `/v1/models` endpoint, verifies the pinned CLIProxyAPI archive, generates standard YAML, and enables the systemd user service. It also enables lingering for the current user. If system policy rejects lingering, the installer reports that failure; your administrator may need to allow `loginctl enable-linger` for your account to provide startup before login.

Port 8317 must be free for a fresh install. The installer does not replace an unrelated CLIProxyAPI configuration, service, or command. To use another port:

```bash
python3 install.py --port 8417
```

The supported release archives are pinned separately for Linux x86_64 and aarch64. Downloads are verified before any executable is installed. Archive paths are not extracted into the filesystem; only the expected binary and license are copied into the managed release directory.

## Files

Default locations are below. `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and `XDG_CACHE_HOME` override their respective roots.

| Location | Purpose |
| --- | --- |
| `~/.config/cliproxyapi/config.yaml` | Normal CLIProxyAPI configuration, including the upstream token; mode `0600` |
| `~/.config/cliproxyapi/proxy-key` | Local client authentication key; mode `0600` |
| `~/.config/cliproxyapi/kondash-install.json` | Installer ownership/version metadata; no tokens |
| `~/.config/cliproxyapi/auth/` | CLIProxyAPI credential directory |
| `~/.local/share/cliproxyapi/releases/` | Official binary and license, organized by version |
| `~/.local/share/cliproxyapi/cli-proxy-api` | Active release symlink |
| `~/.local/bin/cli-proxy-api` | Optional command-path convenience symlink installed by setup |
| `~/.cache/cliproxyapi/` | Checksum-verified release archive cache |
| `~/.config/systemd/user/cliproxyapi.service` | Installed user unit with concrete local paths |

The private config/data/cache directories use mode `0700`. No credentials are stored in this checkout. The service executes the upstream binary directly; no Python process, repo symlink, or custom runtime configuration generator is required to keep it running.

## Service commands

```bash
systemctl --user status cliproxyapi.service
systemctl --user restart cliproxyapi.service
systemctl --user stop cliproxyapi.service
journalctl --user -u cliproxyapi.service -n 50 --no-pager
```

The service remains running when a client exits. It restarts on process failure, subject to the unit's restart-rate limit. Enabled lingering allows the user-service manager to start at boot and remain after logout. A reboot was not needed to perform installation.

The service listens only on IPv4 loopback and requires a local Bearer token. The management API and web UI (`/management.html`) are enabled for loopback clients only (`allow-remote: false`) and require the password in `~/.config/cliproxyapi/management-key`. WSL's localhost forwarding makes the UI reachable from the Windows host at the same URL. CLIProxyAPI downloads the UI page from GitHub on first use and replaces the plaintext password in `config.yaml` with a bcrypt hash. Application logs (no request bodies) are written to `~/.config/cliproxyapi/auth/logs/` and shown in the web UI. CLIProxyAPI only offers size-based limits (capped at 100 MB here), so `cliproxyapi-log-cleanup.timer` deletes rotated log files older than 30 days once a day. The active `main.log` rotates by size only, so on a quiet machine it can hold lines older than 30 days. Request-body logging is disabled; any logs still deserve review before sharing.

## Reinstall, rotate a key, or refresh models

Rerun `python3 install.py` to install the pinned release and service again while preserving the existing YAML and local key. It does not need the upstream key again when preserving an existing managed configuration.

To set a new upstream key or refresh model discovery:

```bash
python3 install.py --reconfigure
# Or:
python3 install.py --reconfigure --api-key-file /path/to/private/new-token
```

Reconfiguration replaces the generated KONDash configuration and creates a private timestamped backup. If local YAML edits are detected, setup refuses to replace them unless `--replace-config` is also supplied. This prevents accidentally discarding additional providers or other customizations. Backups contain credentials and must remain private.

You may edit the YAML directly as with any normal CLIProxyAPI instance, then restart the service. The local `proxy-key` file must match the key in `access.api-keys` and the loopback bridge credential. The reconfiguration command keeps these values consistent automatically.

## Uninstall

```bash
python3 install.py --uninstall
```

This stops and disables the managed service, removes its unit and command symlink, and deletes installed releases and the download cache. Private configuration and credentials are kept so a later installation can reuse them. Unmanaged configurations, units, and commands are refused before any removal.

For a completely fresh installation next time:

```bash
python3 install.py --uninstall --purge
```

`--purge` also deletes the managed configuration directory, including the upstream token stored in YAML, generated client key, auth files, and configuration backups. It never deletes the original external file supplied with `--api-key-file`, the repository, or backups outside the managed directory. Repeating an uninstall after removal succeeds without making changes.

Lingering is an account-wide setting and is left enabled for other user services. A fresh install is started manually with `python3 install.py`; uninstall never reinstalls or starts a replacement instance.

## Troubleshooting

- **Cannot connect to the user-service manager:** run setup from a normal logged-in user session with systemd user services available.
- **HTTP 401 from model discovery:** the upstream token was rejected. The installer deliberately does not print the response body.
- **No enabled models:** the token needs access to at least one deployment before setup can build the provider routes.
- **Port occupied or unmanaged installation found:** stop or relocate the existing instance; setup will not silently take it over.
- **Download checksum mismatch:** no executable is installed from that archive. Remove a corrupt cached archive and retry, or investigate the pinned release change.
- **Local client receives HTTP 401:** use the local proxy key, rather than the upstream KONDash token.
