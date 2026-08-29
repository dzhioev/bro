# bro

> [!WARNING]
> **Early development
> — not recommended for production use.**
>
> The framework is at an early stage:
> much of it is work in progress, more is still to be designed, and `master` regularly carries breaking changes that require migrations.
> Interfaces move without deprecation cycles or compatibility shims.
>
> It is provided as-is, without warranty or support of any kind, and no responsibility is taken for any outcome of running it.
> Use it at your own risk.

Harness your bros:
`bro` is a meta-harness for declarative agent personas.
A persona — system prompt, tools, data sources, credentials, scripts
— is declared once and runs unchanged on every supported harness:
a Claude Code session, or the framework's own native agent loop.
Around that core:
MCP tool serving, credential scoping, recorded runs, task-driven development workflows, and `ride`
— a unified harness runtime for isolated host or container workspaces.
Consumer projects install the distribution, register their extensions through entry points, and choose their defaults in `[tool.bro]`.
[`DESIGN.md`](DESIGN.md) covers the conceptual model.

**Launch commands:** `ride solo` / `ask` and `ride along` / `call` create managed isolated workspaces.
`bro run` and `bro chat` run in the calling process with ambient credentials;
they do not isolate or hydrate a scoped store.

The repository is a [uv](https://docs.astral.sh/uv/) workspace:
the root publishes `bro`, [`native/`](native/README.md) publishes the native engine, [`ride/`](ride/README.md) publishes the managed-workspace runtime and both harness adapters,
[`dev/`](dev/README.md) publishes development tooling, and [`oops/`](oops/README.md) publishes deployment and operations machinery for consumer repositories.

## Prerequisites

Development requires Python 3.12 or newer, Git, and [uv](https://docs.astral.sh/uv/).
`ride` sessions using the Claude harness also require Claude Code;
container workspaces require Docker, GitHub workflows require `gh`, and benchmark runs additionally require Docker's `compose` CLI plugin, which installs separately from the engine.
`bro/setup/setup_env.sh` is an opinionated macOS/Ubuntu reference installer for these host tools, not part of repository provisioning.

## Installation

The base `bro` distribution provides the declaration and inspection APIs, MCP abstraction, credential handling, shared workspace/session primitives, prompts, and framework services.
Install the distribution for the engine you run:

- `bro-native` — the native LLM loop, the `bro` command (`list`, `show`, `run`, and `chat`), provider clients, and terminal UIs
- `bro-ride` — the managed-workspace runtime, both harness adapters, `ask` / `call` aliases, and `dive-in`
- `bro-oops` — deployment and operations helpers for repositories that deploy bro services
- `bro[http]` — aiohttp-based clients and services
- `bro[llm]` — OpenAI LLM access without the agent UI dependencies
- `bro[runtime]` — the MCP serving front, over stdio or HTTP
- `bro[trails-server]` — the aiohttp trails proxy and optional DynamoDB/S3 backend
- `bro[aws]` — the `ssm` credential source
- `bro[github]` — GitHub App authentication

A repository operated by `ride` may provide a root `setup.sh` to provision its own environment;
an absent script is logged and skipped.
Session machinery always comes from the invoking installation's frozen runtime bundle
— a host snapshot or a read-only container volume
— so the project environment need not provide `ride` or `bro`.
The ride installation itself must provide every persona, extension, and engine a session uses, including `bro-native` for `--harness bro`.
Managed-session PATH contains pinned session commands plus system tools;
repository commands use `uv run` or `.venv/bin/`.
A consuming development repository normally installs `bro-dev` in its dev dependency group and calls `bro.dev.install` during setup to install the commit-footer hooks and `git golc` alias.
When the container entrypoint links the optional project dependency bake into the tree it exports `RIDE_VENV_MANIFEST`, whose staged manifests let setup reuse the environment until the tree's copies diverge.

> [!NOTE]
> **Upgrade from checkout-keyed runtime state:** the first outer `ride`, `ask`, or `call` command migrates existing project-keyed workspaces, trails, summon audit, and broker state into the global runtime root.
> See [`bro/reference/ride.md`](bro/reference/ride.md#runtime-state) for the migration contract.

## Extension entry points

Installed distributions contribute framework extensions through standard Python entry-point groups:

- `bro` — personas, keyed by persona name
- `bro.credential_sources` — credential minting source classes, keyed by source type
- `bro.credentials` — credential registry fragments
- `bro.brog.backends` — task-tracker backends, keyed by backend name
- `bro.toolsets` — standalone MCP `Toolset` objects, keyed by namespace
- `bro.mcp.targets` — assembled MCP target resolvers, keyed by target prefix
- `bro.session_commands` — console scripts exposed on managed-session PATH
- `bro.broker_kinds` — broker request kinds served by every managed session's host, keyed by kind name;
  each entry targets a factory `(workspace_tree) -> handler`

Entry-point metadata is written when a distribution is installed, so adding or removing a declaration requires another `uv sync`;
editing an already-declared target does not.
Name-keyed groups load only the selected entry.
Credential registry assembly loads every `bro.credentials` contributor, so those target modules must remain cheap to import.
Each target is a dictionary with a required one-line `description` and an optional registry-format `install` hook;
source paths, instances, and other fields are rejected.
A `bro.credential_sources` target is a `MintingSource` subclass whose `TYPE` matches its entry-point name.
Its inherited `from_dict` receives only that type's parameters from `creds.json` (the `type` discriminator and convention material path are owned by the store), and `mint(config)` derives a value from the JSON object in `creds/<name>.cred`.
Broker composition loads every `bro.broker_kinds` contributor, so those target modules must remain cheap to import.
Each `bro.session_commands` entry repeats the name and target of a console script from the same distribution;
materialization rejects missing, mismatched, or duplicate declarations.

## Project configuration

Every repository attached with `--repo` declares its launch defaults;
detached sessions read no project configuration:

```toml
[tool.bro]
default = "dev"
harness = "claude"                        # optional ride default; claude when omitted
image-repository = "bro/example"          # optional; defaults to bro/<default>
build-context-command = "git ls-files"    # optional session-image context file list
```

`default` is required.
`harness` accepts `claude` or `bro`.
Unknown keys and malformed values fail at config load rather than being ignored.

## Trails storage

Recording is mandatory, and storage is local unless configured otherwise:
with no `~/.bro/trails.json`, a run writes to the global `trails` directory in the runtime state root (`bro/reference/ride.md`, "Runtime state").
Container launch composers bind-mount that host root at the fixed absolute `/var/ride/trails` path inside the container automatically.

The hosted service is the opt-in, `{"backend": "service", "base_url": "https://trails.example", "token": "<bearer>"}`;
an existing config with `base_url` and `token` but no `backend` continues to select the service, and `{"backend": "local"}` states the default explicitly.
`trails-server` resolves its hosted store from the same credential vocabulary, selecting either local storage or the DynamoDB/S3 shape documented in [`bro/setup/AGENTS.md`](bro/setup/AGENTS.md)
— but it requires the credential rather than defaulting, since a server states the backend it serves;
only its bearer-auth settings remain command-line/environment flags.

## Development

```bash
./setup.sh
source .venv/bin/activate
./format.sh
run-tests
```

`./setup.sh` syncs the editable workspace and installs the repository hooks;
the formatter and the test gate cover every workspace member, plus [`benchmark/`](benchmark/README.md), which ships from this repository beside the workspace rather than inside it and carries an environment of its own.
Build the workspace wheels with `uv build --package bro`, `uv build --package bro-native`, `uv build --package bro-dev`, `uv build --package bro-oops`, and `uv build --package bro-ride`.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
