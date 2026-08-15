# bro

> [!WARNING]
> **Early development — not recommended for production use.**
>
> The framework is at an early stage: much of it is work in progress, more is still to be designed, and `master` regularly carries breaking changes that require migrations. Interfaces move without deprecation cycles or compatibility shims.
>
> It is provided as-is, without warranty or support of any kind, and no responsibility is taken for any outcome of running it. Use it at your own risk.

Harness your bros: `bro` is a meta-harness for declarative agent personas. A persona — system prompt, tools, data sources, credentials, scripts — is declared once and runs unchanged on every supported harness: a Claude Code session, or the framework's own native agent loop. Around that core: MCP tool serving, credential scoping, recorded runs, task-driven development workflows, and `cw` — isolated host or container workspaces. Consumer projects install the distribution, register their extensions through entry points, and choose their defaults in `[tool.bro]`. [`DESIGN.md`](DESIGN.md) covers the conceptual model.

The repository is a [uv](https://docs.astral.sh/uv/) workspace: this root publishes `bro`, and [`dev/`](dev/README.md) publishes development tooling for repositories built on the framework — console-script metadata generation, token-usage reports, shell-policy checks, and repository hook installation.

## Prerequisites

Development requires Python 3.12 or newer, Git, and [uv](https://docs.astral.sh/uv/). `cw` sessions also require Claude Code; container workspaces require Docker, and GitHub workflows require `gh`. `bro/setup/setup_env.sh` is an opinionated macOS/Ubuntu reference installer for these host tools, not part of repository provisioning.

## Installation

The base distribution contains every module — `bro.base`, the MCP abstraction, credential handling, workspace primitives, prompts, and the framework console scripts — and its required dependencies cover declaring and inspecting a persona: `bro list`, `bro show`, `credentials`, and `bro-shell-dir` run on a bare install. Every surface that *runs* a persona states its dependencies in an extra, so an install pays for the surfaces it selects:

- `bro[agent]` — the OpenAI agent loop, tool serving, data sources, and terminal UIs
- `bro[cw]` — interactive `cw` and launch UI dependencies
- `bro[http]` — aiohttp-based clients and services
- `bro[llm]` — OpenAI LLM access without the agent UI dependencies
- `bro[runtime]` — the MCP serving front, over stdio or HTTP
- `bro[trails-server]` — the aiohttp/DynamoDB trails service
- `bro[aws]` — the `ssm` credential source
- `bro[github]` — GitHub App authentication

A repository operated by `cw` provides a root `setup.sh` whose postcondition is an executable `.venv/bin/cw`. When the container entrypoint links a pre-built environment into the tree it exports `CW_VENV_MANIFEST`, a directory holding the dependency manifests that environment was resolved from at their repository-relative paths; the script must reuse the environment while the tree's own copies still match them and sync when they diverge. A consuming development repository normally installs `bro-dev` in its dev dependency group, syncs the workspace, activates the resulting venv, and calls `bro.dev.install` to install the commit-footer hooks and `git golc` alias.

## Extension entry points

Installed distributions contribute framework extensions through standard Python entry-point groups:

- `bro` — personas, keyed by persona name
- `bro.credential_sources` — credential minting source classes, keyed by source type
- `bro.credentials` — credential registry fragments
- `bro.brog.backends` — task-tracker backends, keyed by backend name
- `bro.toolsets` — standalone MCP `Toolset` objects, keyed by namespace

Entry-point metadata is written when a distribution is installed, so adding or removing a declaration requires another `uv sync`; editing an already-declared target does not. Name-keyed groups load only the selected entry. Credential registry assembly loads every `bro.credentials` contributor, so those target modules must remain cheap to import.

## Project configuration

Every operated repository declares its launch defaults:

```toml
[tool.bro]
default = "dev"
image-repository = "bro/example"          # optional; defaults to bro/<default>
build-context-command = "git ls-files"    # optional session-image context file list
```

`default` is required. Unknown keys and malformed values fail at config load rather than being ignored.

## Development

```bash
./setup.sh
source .venv/bin/activate
./format.sh
run-tests
```

`./setup.sh` syncs the editable workspace and installs the repository hooks; the formatter and the test gate cover every workspace member. Build the wheels with `uv build --package bro` and `uv build --package bro-dev`.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
