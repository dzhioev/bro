# bro

`bro` is a Python agent framework and workspace launcher. It provides declarative agent composition, MCP tool serving, credential scoping, recorded runs, task-driven development workflows, and `cw` host or container workspaces. Consumer projects install the distribution, register their extensions through entry points, and choose their defaults in `[tool.bro]`.

## Prerequisites

Development requires Python 3.12 or newer, Git, and [uv](https://docs.astral.sh/uv/). `cw` sessions also require Claude Code; container workspaces require Docker, and GitHub workflows require `gh`. `bro/setup/setup_env.sh` is an opinionated macOS/Ubuntu reference installer for these host tools, not part of repository provisioning.

## Installation

The base distribution contains `bro.base`, the MCP abstraction, credential handling, workspace primitives, prompts, and the framework console scripts. Optional dependency groups install the heavier surfaces:

- `bro[agent]` — the OpenAI agent loop, data sources, and terminal UIs
- `bro[cw]` — interactive `cw` and launch UI dependencies
- `bro[http]` — aiohttp-based clients and services
- `bro[llm]` — OpenAI LLM access without the agent UI dependencies
- `bro[runtime]` — HTTP MCP serving through Starlette and uvicorn
- `bro[trails-server]` — the aiohttp/DynamoDB trails service
- `bro[github]` — GitHub App authentication

A repository operated by `cw` provides a root `setup.sh` whose postcondition is an executable `.venv/bin/cw`. When the container entrypoint exports `CW_VENV_BAKED=1`, the script must reuse the baked environment instead of syncing it. A consuming development repository normally installs `bro-dev` in its dev dependency group, syncs the workspace, activates the resulting venv, and calls `bro-dev.install` to install the post-commit hook and `git golc` alias.

## Extension entry points

Installed distributions contribute framework extensions through standard Python entry-point groups:

- `bro` — personas, keyed by persona name
- `bro.credential_sources` — credential minting source classes, keyed by source type
- `bro.credentials` — credential registry fragments
- `bro.brog.backends` — task-tracker backends, keyed by backend name
- `bro.toolsets` — standalone MCP toolsets, keyed by namespace

Entry-point metadata is written when a distribution is installed, so adding or removing a declaration requires another `uv sync`; editing an already-declared target does not. Name-keyed groups load only the selected entry. Credential registry assembly loads every `bro.credentials` contributor, so those target modules must remain cheap to import.

## Project configuration

Every operated repository declares its launch defaults:

```toml
[tool.bro]
default = "dev"
image-repository = "bro/example"          # optional; defaults to bro/<default>
creds = { brog = "github" }               # optional kind-to-instance selection
footer-command = "bro-dev.claude-commit-footer"  # optional squash footer producer
```

`default` is required. Unknown keys and malformed values fail at config load rather than being ignored.

## Development

From this workspace's repository root, `uv sync --all-packages --all-groups --all-extras` installs the three editable members. The framework member owns its own gates:

```bash
cd bro
./format.sh
./run-tests
```

Build the distributable wheel from the workspace root with `uv build --package bro`.
