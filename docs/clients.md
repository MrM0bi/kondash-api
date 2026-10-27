# Coding-agent clients

Install the gateway with `python3 install.py`, then run:

```bash
python3 configure_agents.py --dry-run
python3 configure_agents.py
```

The script uses the running local gateway to discover models. It configures supported clients found on `PATH`; it does not install clients or start the gateway. Clients that are not installed are skipped. By default, an installed client that is too old (Codex before 0.164.0, or OpenCode v1 with explicit reasoning levels) is skipped with a warning and the others are still configured. Explicitly requesting a missing or incompatible client is an error. To configure a subset:

```bash
python3 configure_agents.py --agents opencode pi
python3 configure_agents.py --agents codex claude
```

All selected clients are checked before any configuration is written. The default gateway URL is `http://127.0.0.1:8317/v1`; a custom port is read from the gateway installation metadata. Overrides are available with `--base-url http://127.0.0.1:PORT/v1` and `--proxy-key-file /path/to/private/proxy-key`. Only HTTP localhost URLs are accepted, and discovery does not follow redirects or use HTTP proxy environment variables.

## Activate a provider

- **OpenCode:** restart OpenCode and any existing background server, then select **KONDash** in `/models`. The provider ID is `kondash`.
- **Pi:** select **KONDash** in `/model`. The provider ID is `kondash`.
- **Codex:** run `codex --profile kondash`.
- **Claude Code:** run `~/.local/bin/claude-kondash`, or `claude-kondash` if `~/.local/bin` is on your `PATH`.

OpenCode and Pi retain their previous default model. Codex and Claude use the gateway only when launching their separate profile. To choose the starting deployment for those profiles, pass `--model '<deployment-id>'` with the raw deployment ID, without the `-proxy` suffix. By default the script selects the first matching Codex catalog entry, or the first discovered deployment when Codex is excluded.

## Files and existing configuration

The following are default paths. `XDG_CONFIG_HOME`, `CODEX_HOME`, and `PI_CODING_AGENT_DIR` are respected.

- OpenCode: merges the `kondash` provider into `~/.config/opencode/opencode.jsonc` if present, otherwise `opencode.json`. Other providers, defaults, and unrelated JSONC comments are retained. OpenCode v2 uses its Responses runtime; v1 uses its OpenAI-compatible Chat runtime and older configuration schema.
- Pi: merges the provider into `~/.pi/agent/models.json`, using its `openai-responses` adapter. If `auth.json` already contains a credential for this provider, replacing it requires `--replace`; other credentials remain intact.
- Codex: creates `~/.codex/kondash.config.toml` and a private catalog under `~/.config/cliproxyapi/agents/`. It leaves the base `config.toml` unchanged.
- Claude Code: creates private settings under `~/.config/cliproxyapi/agents/` and a `~/.local/bin/claude-kondash` launcher. Global Claude settings remain unchanged. The launcher clears conflicting inherited provider credentials and cloud routing flags in its child process, then loads the gateway settings. The Opus/Sonnet/Haiku slots all route to the selected deployment; these slot names do not identify the underlying model.

The configurations reference the local `proxy-key` file through file substitution or a credential helper; they do not copy its contents. The upstream KONDash token is never needed by this script. Private generated files use mode `0600` and the Claude launcher uses `0700`. Launchers and profiles do not depend on this checkout afterward.

Existing files are backed up privately under `~/.config/cliproxyapi/agents/backups/before-*/` before modification. Each backup has a `restore-paths.json` recording the original destinations and modes. `--dry-run` creates neither files nor backups. To restore manually, stop the affected client and copy the selected backup files to those destinations; remove newly generated profiles if needed.

Re-running setup refreshes entries it owns, including discovered models. An unrelated existing KONDash entry or a locally edited generated entry/profile requires an explicit replacement:

```bash
python3 configure_agents.py --replace
```

That replacement still backs up the existing files and preserves unrelated settings. Run setup again after `install.py --reconfigure` changes available deployments. Removing a provider from the gateway does not automatically remove it from agent configuration until setup is rerun.

Gateway uninstall preserves agent settings. `install.py --uninstall --purge` deletes the private gateway configuration, key, catalogs, Claude settings, and backups; provider entries and launchers in agent directories remain and will need reconfiguration or manual removal.

## Codex model metadata

Automatic Codex setup requires **Codex 0.164.0 or newer**. It reads the CLI's offline bundled catalog, keeps entries whose IDs match discovered deployments, preserves their model instructions, and adapts tool/capability metadata for this gateway. It uses the `<deployment-id>-proxy` compatibility route with `wire_api = "responses"`; Pi, OpenCode, and Claude use the plain `<deployment-id>` routes.

Unknown deployment aliases cannot safely be assigned another model's instructions. If no catalog entry matches, setup stops before writing any selected client's files. Either exclude Codex with `--agents`, or supply a private, deployment-specific catalog:

```bash
python3 configure_agents.py --codex-catalog /path/to/private/models.json
```

That file must contain a `models` array with matching `slug` values and valid metadata/instructions for the actual models. Use the [Codex model catalog documentation](https://learn.chatgpt.com/docs/model-catalogs) and `codex debug models --bundled` as a format reference. Do not commit a deployment-specific catalog or blindly rename an unrelated model's entry.

Gateway capabilities vary by deployment. Generated settings disable unsupported Codex search/remote compaction/code mode behavior and use conservative text-only context/output limits. Reasoning, images, large contexts, and advanced agent features require separate validation. Those limits are client caps, not discovered deployment specifications.

## Thinking and effort

Pi and OpenCode v2 get thinking controls (low, medium, high, xhigh, max) for every discovered deployment by default. To limit or disable them for one deployment:

```bash
python3 configure_agents.py --model '<deployment-id>' --reasoning-efforts low high   # only these levels
python3 configure_agents.py --model '<deployment-id>' --reasoning-efforts            # no thinking controls
```

This choice is remembered in the private setup manifest and retained when refreshing models; `install.py --uninstall --purge` deletes it. As of 2026-10-27, KONDash accepts but ignores the effort value, so the levels change the request but not the model's behavior. OpenCode v1 has no automatic reasoning setup in this script.

Codex retains the selected model's catalog levels and accepts, for example, `codex --profile kondash -c 'model_reasoning_effort="high"'`. Pi accepts `pi --provider kondash --model '<deployment-id>' --thinking high`. OpenCode v2 accepts `opencode run --model 'kondash/<deployment-id>#high'`. Claude Code accepts `claude-kondash --effort high`. Use a level the model and client support; a successful settings change alone does not demonstrate upstream reasoning support.

## MCP

MCP servers belong to each coding agent, independently of its model provider. Client-defined MCP tools pass through the same function-tool loop as coding tools. This script preserves existing MCP settings; it does not install or approve third-party servers.

Use the agent's normal MCP configuration. Pi can expose small servers directly with `"exposure": "direct"`; its codemode/deferred exposure needs the relevant discovery tools enabled. When using an explicit Pi `--tools` list, include the server's `mcp__<server>__*` tools and resource tools as needed. In noninteractive Codex runs, the server/tool approval policy must allow the intended tool. An approval rejection is distinct from an API-format failure. The test runner grants access only to its harmless synthetic fixture server in its isolated profile.

## Compatibility checks

The generated configurations were exercised with Codex **0.164.0**, Pi **1.4.0**, OpenCode **2.2.0**, and Claude Code **2.1.311** against a temporary synthetic localhost gateway. Each client loaded its configuration, authenticated, and completed a streamed text response. Automated tests also cover the OpenCode v1 configuration schema, configuration merging, conflict detection, backups, and rollback. This does not verify every upstream model's behavior or tool support; OpenCode v1 has configuration tests only.

Live gateway checks also exercised local code edits, file creation, terminal execution and assertions, a stdio MCP tool returning an unpredictable receipt, and MCP resource reads in each of the four agents. Pi's explicit `read`, `write`, `edit`, `bash`, `grep`, `find`, and `ls` tools were exercised. Codex used its command and patch tools; OpenCode used read/search/glob/patch/shell and its tool execution bridge; Claude used its currently exposed read/edit/terminal tools. Tool names and availability depend on the client version. This is representative coding/MCP coverage, not certification of every tool or every deployment.

For the selected live deployment, low, medium, high, xhigh, and max requests completed in all four clients, and the requested effort appeared in client-to-gateway request controls. Pi required the explicit reasoning opt-in described above. This establishes forwarding and request acceptance. It does not prove that upstream inference uses distinct internal reasoning budgets; gateways can translate or normalize settings, and token accounting can be incomplete.

To repeat the live checks yourself:

```bash
python3 test_agents.py --phase coding
python3 test_agents.py --phase effort --efforts low medium high xhigh max --enable-reasoning
```

These are opt-in **real, potentially billable model requests**. The runner uses separate temporary homes, workspaces, and recording proxies. It does not modify normal agent settings or install/restart the gateway. The test's MCP server has no external dependencies. Agent output and sanitized request summaries stay in a private temporary directory outside the repository; paths are printed so you can inspect or remove the reports. The summary never includes model IDs, credentials, prompts, or reasoning text. Configure another deployment with `--model`, select clients with `--agents`, or use `--codex-catalog` for a private alias catalog. The default live-test URL uses port 8317; use `--base-url` for a custom port. Results print as a readable report by default; add `--format json` for one JSON object per agent on stdout (the intro line goes to stderr). The exit code is 0 only when every selected check passes.

Not covered: arbitrary third-party MCP servers, OAuth flows, all MCP exposure modes, hosted web/file search, images, large-context stress tests, compaction, subagents, interactive approval UI, and reasoning quality or cost benchmarks. These require their own configuration and tests.

Claude Code uses the gateway's Anthropic Messages translation at `http://127.0.0.1:8317`. API translation does not make another vendor's model a Claude model. Anthropic's supported gateway scope is Claude models; model-specific context limits, cost estimates, and advanced features may be inaccurate with other models.

## Manual examples and sources

Sanitized examples are available for [Pi](../examples/pi.models.example.json), [OpenCode v2](../examples/opencode.v2.example.json), [Codex](../examples/codex.example.toml), and [Claude Code](../examples/claude.settings.example.json).

- [Pi model configuration](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models-configuration.md)
- [OpenCode v2 providers](https://opencode.ai/v2/docs/providers/) and [configuration](https://opencode.ai/v2/docs/config/)
- [OpenCode v1 providers](https://opencode.ai/docs/providers/)
- [Codex gateway setup](https://learn.chatgpt.com/docs/enterprise/connect-to-a-gateway) and [model catalogs](https://learn.chatgpt.com/docs/model-catalogs)
- [Claude Code gateway setup](https://code.claude.com/docs/en/llm-gateway-connect)
- [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference), [OpenCode models](https://opencode.ai/v2/docs/models/), [Pi MCP servers](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/mcp.md), and [Claude Code effort settings](https://code.claude.com/docs/en/model-config)
