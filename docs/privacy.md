# Public repository and private installation data

The KONDash API base URL is an intentional public default. Authentication material, organization-specific model IDs, and local installation details belong in the private installation, not in this repository.

The installer never prints tokens, accepts a token literal as an argument, or stores credentials in the checkout. It keeps configuration under the user's XDG directories with private permissions. Authenticated model discovery prints only a count; the actual IDs are written to the private local YAML.

The optional `configure_agents.py` reads only the separate local proxy key and queries the localhost gateway. Generated agent profiles, model IDs, Codex catalogs, ownership metadata, and backups stay outside the checkout. Credential helpers and file substitutions reference the existing private local key rather than copying its contents. Setup prints client names and model counts, not deployment IDs or API responses.

The opt-in `test_agents.py` live checks keep agent logs and reports in a private temporary directory outside the checkout. Request summaries retain tool names, HTTP status codes, and control values; they omit model IDs, credentials, prompt/response bodies, and reasoning text. Full client logs remain private and should be reviewed before sharing. Remove the printed temporary report directory when you no longer need it.

Do not commit:

- Generated `config.yaml`, local proxy keys, authentication files, or configuration backups.
- Model-list responses or catalogs obtained using organization credentials.
- Raw request/response logs, transcripts, trace IDs, personal filesystem paths, account names, or machine-specific test reports.

Examples and automated tests use placeholders or clearly synthetic data. The ignore file covers common credential/configuration artifacts, but it cannot identify every private filename. Review changes before publishing.

Use synthetic prompts and files for reproducible bug reports. Include the CLIProxyAPI/client versions and a sanitized error category or HTTP status. Review any log carefully; disabling request logging is not a guarantee that every diagnostic is suitable for public posting.

The downloaded CLIProxyAPI binary retains its upstream license in the private release directory.
