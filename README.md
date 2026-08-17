# bro

> [!WARNING]
> **Early development — not recommended for production use.**
>
> The framework is at an early stage: much of it is work in progress, more is still to be designed, and `master` regularly carries breaking changes that require migrations. Interfaces move without deprecation cycles or compatibility shims.
>
> It is provided as-is, without warranty or support of any kind, and no responsibility is taken for any outcome of running it. Use it at your own risk.

Harness your bros: `bro` is a meta-harness for declarative agent personas. A persona — system prompt, tools, data sources, credentials, scripts — is declared once and runs unchanged on every supported harness: a Claude Code session, or the framework's own native agent loop. Around that core: MCP tool serving, credential scoping, recorded runs, task-driven development workflows, and `ride` — a unified harness runtime for isolated host or container workspaces. Consumer projects install the distribution, register their extensions through entry points, and choose their defaults in `[tool.bro]`. [`DESIGN.md`](DESIGN.md) covers the conceptual model.

**Launch commands:** `ride solo` / `ask` and `ride along` / `call` create managed isolated workspaces. `bro run` and `bro chat` run in the calling process with ambient credentials; they do not isolate or hydrate a scoped store.

The repository is a [uv](https://docs.astral.sh/uv/) workspace: the root publishes `bro`, [`ride/`](ride/README.md) publishes the managed-workspace runtime and Claude harness, and [`dev/`](dev/README.md) publishes development tooling for repositories built on the framework.

## Prerequisites

Development requires Python 3.12 or newer, Git, and [uv](https://docs.astral.sh/uv/). `ride` sessions using the Claude harness also require Claude Code; container workspaces require Docker, GitHub workflows require `gh`, and benchmark runs additionally require Docker's `compose` CLI plugin, which installs separately from the engine. `bro/setup/setup_env.sh` is an opinionated macOS/Ubuntu reference installer for these host tools, not part of repository provisioning.

## Installation

The base distribution contains every module — `bro.base`, the MCP abstraction, credential handling, workspace primitives, prompts, and the framework console scripts — and its required dependencies cover declaring and inspecting a persona: `bro list`, `bro show`, `credentials`, and `bro-shell-dir` run on a bare install. Every surface that *runs* a persona states its dependencies in an extra, so an install pays for the surfaces it selects:

- `bro[agent]` — the OpenAI agent loop, tool serving, data sources, and terminal UIs
- `bro-ride` — the `ride` runtime, Claude harness, `ask` / `call` aliases, and `dive-in` (it installs its `bro` runtime extras)
- `bro[http]` — aiohttp-based clients and services
- `bro[llm]` — OpenAI LLM access without the agent UI dependencies
- `bro[runtime]` — the MCP serving front, over stdio or HTTP
- `bro[trails-server]` — the aiohttp trails proxy and optional DynamoDB/S3 backend
- `bro[aws]` — the `ssm` credential source
- `bro[github]` — GitHub App authentication

A repository operated by `ride` provides a root `setup.sh` whose postcondition is executable `.venv/bin/ride` and a user-owned `/var/ride/<project-key>` runtime root. A consuming development repository normally installs `bro-dev` in its dev dependency group, syncs the workspace, activates the resulting venv, and calls `bro.dev.install`; on the host, that installer uses `sudo` once to create the checkout-keyed runtime root, then installs the commit-footer hooks and `git golc` alias. When the container entrypoint links a pre-built environment into the tree it exports `RIDE_VENV_MANIFEST`, a directory holding the dependency manifests that environment was resolved from at their repository-relative paths; the script must reuse the environment while the tree's own copies still match them and sync when they diverge.

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
harness = "claude"                        # optional ride default; claude when omitted
image-repository = "bro/example"          # optional; defaults to bro/<default>
build-context-command = "git ls-files"    # optional session-image context file list
```

`default` is required. `harness` accepts `claude` or `bro`. Unknown keys and malformed values fail at config load rather than being ignored.

## Trails storage

Recording is mandatory, and storage is local unless configured otherwise: with no `~/.bro/trails.json`, a run writes to `/var/ride/<project-key>/trails`, beside the checkout's other runtime state. Container launch composers bind-mount that host root at the fixed absolute `/var/ride/trails` path inside the container automatically.

The hosted service is the opt-in, `{"backend": "service", "base_url": "https://trails.example", "token": "<bearer>"}`; an existing config with `base_url` and `token` but no `backend` continues to select the service, and `{"backend": "local"}` states the default explicitly. `trails-server` resolves its hosted store from the same credential vocabulary, selecting either local storage or the DynamoDB/S3 shape documented in [`bro/setup/CLAUDE.md`](bro/setup/CLAUDE.md) — but it requires the credential rather than defaulting, since a server states the backend it serves; only its bearer-auth settings remain command-line/environment flags.

## Development

```bash
./setup.sh
source .venv/bin/activate
./format.sh
run-tests
```

`./setup.sh` syncs the editable workspace and installs the repository hooks; the formatter and the test gate cover every workspace member, plus [`benchmark/`](benchmark/README.md), which ships from this repository beside the workspace rather than inside it and carries an environment of its own. Build the wheels with `uv build --package bro` and `uv build --package bro-dev`.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
