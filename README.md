# bro

Harness your bros: `bro` is a meta-harness for declarative agent personas. A persona — system prompt, tools, data sources, credentials, scripts — is declared once and runs unchanged on every supported harness: a Claude Code session, or the framework's own native agent loop. Around that core: MCP tool serving, credential scoping, recorded runs, task-driven development workflows, and `cw` — isolated host or container workspaces.

The repository is a [uv](https://docs.astral.sh/uv/) workspace with two distributions:

- [`bro/`](bro/README.md) — the framework: agents ("bros"), the MCP abstraction, credentials, workspaces, run trails, and the `cw` launcher. Its README covers installation, extras, and the extension entry points; [`bro/DESIGN.md`](bro/DESIGN.md) covers the conceptual model.
- [`bro-dev/`](bro-dev/README.md) — development tooling for repositories built on the framework: console-script metadata generation, commit token footers, shell-policy checks, repository hooks, and the `bro-dev` persona.

## Development

```bash
./setup.sh
source .venv/bin/activate
```

Each member owns its gates:

```bash
(cd bro && ./format.sh && ./run-tests)
(cd bro-dev && ./format.sh && ./run-tests)
```

`./run-tests` at the root runs both suites; `./format.sh` formats both members. Build wheels from the root with `uv build --package bro` and `uv build --package bro-dev`.

## History

This repository was exported from a private monorepo, carrying the framework's commit history with it. Every commit ends with a `> rewritten from a private repository` disclaimer naming its original; task links are redacted to opaque `N:<id>` markers, and agent identities are collapsed to `bbbbro[bot]`.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
