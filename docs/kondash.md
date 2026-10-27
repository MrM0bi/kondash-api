# KONDash API and compatibility

KONDash provides authenticated access to organization-enabled model deployments through an OpenAI-compatible API. This integration uses:

```text
https://kondash.konverto.eu/v1
```

Send the upstream token as `Authorization: Bearer <token>`. Available models depend on the token's organization and permissions. The installer calls `GET /v1/models` and uses those exact returned IDs; it does not ship an organization-specific model list.

## Endpoints

The KONDash instructions used to design this integration document Chat Completions and Responses, along with optional Files, Vector Stores, and hosted search tools. This project focuses on the model/tool loop needed by coding clients. Access to hosted tools and retrieval features depends on organization configuration.

Coding-agent compatibility needs more than a successful text response. A model must request a local tool, receive the corresponding result, and continue the conversation. Streaming must deliver the expected terminal events. Hosted web/file search support does not establish support for client-defined coding tools.

Different deployments may implement different subsets of the advertised protocols. In particular, string-only Chat message content and limited Responses tool formats can prevent otherwise OpenAI-compatible clients from working directly.

## Provider configuration

The generated YAML contains two normal CLIProxyAPI provider groups:

- The **Responses upstream** group sends requests to KONDash and exposes each discovered deployment under its original ID.
- The **compatibility** group exposes each deployment as `<deployment-id>-proxy` and uses a loopback Chat Completions translation step before reaching that Responses upstream.

Clients that already use compatible function tools can select `<deployment-id>`. Clients needing tool-format normalization can select `<deployment-id>-proxy`. The two sets of names must stay distinct to avoid a routing loop; discovery rejects conflicting IDs.

Both groups run inside one stock CLIProxyAPI process. The extra localhost translation does not itself make another model inference call. It is a compatibility workaround: reasoning metadata, usage accounting, richer content, and advanced tool behavior can differ after translation. Test the actual client/model combination you plan to use.

Automatic image-generation injection is disabled because text-only coding requests should not gain an image tool implicitly. Text input/output is declared conservatively in this profile. These settings are not assertions about every upstream model's capabilities.

## Testing a deployment

Use synthetic data when checking compatibility. A useful coding-agent smoke test asks the client to read a temporary file, write a transformed copy, and run a local assertion that verifies it. Check the resulting file independently rather than relying only on the model's completion message or process exit code.

Text/tool compatibility checks have succeeded with this routing arrangement, but model availability and behavior are deployment-specific. This repository intentionally contains no authenticated catalogs, raw transcripts, organization test reports, pricing claims, or machine-specific benchmark results.

## References

- [CLIProxyAPI project](https://github.com/router-for-me/CLIProxyAPI)
- [CLIProxyAPI v8 configuration](https://help.router-for.me/configuration/options)
- [OpenAI-compatible upstreams](https://help.router-for.me/configuration/provider/openai-compatibility)
- [Codex-compatible Responses upstreams](https://help.router-for.me/configuration/provider/codex-compatibility)
