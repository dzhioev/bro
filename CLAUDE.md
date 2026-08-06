# CLAUDE.md

This file is the project map for Claude Code (claude.ai/code). Member CLAUDE.md files own the detail; only repo-wide policy lives here.

## Project Overview

uv workspace publishing two distributions: `bro` (agent framework and `cw` workspaces) and `bro-dev` (development tooling). Subsystem maps:

- `bro/CLAUDE.md` — the framework (see `bro/DESIGN.md` for the conceptual model); its "Layout" section indexes the per-subsystem maps and reference docs inside the member
- `bro-dev/CLAUDE.md` — development tooling and the `bro-dev` persona

## Commands

Environment first: `./setup.sh` syncs the workspace and installs the repository hooks through `bro-dev`; `source .venv/bin/activate` then puts third-party development tools and every console script on `PATH` as bare commands. The commands below assume the venv is active:

- **Format**: `(cd bro && ./format.sh)` and `(cd bro-dev && ./format.sh)`, or both via `./format.sh`
- **Test**: `(cd bro && ./run-tests)` and `(cd bro-dev && ./run-tests)`, or both via `./run-tests` — the framework suite includes the host-only docker smoke stages (`--no-docker` skips them)
- **Required gate**: both member suites must pass cleanly before changes are pushed
- **Regenerate console scripts**: `sync-scripts --project <member>` after adding or deleting a CLI, then `uv sync --all-packages --all-groups --all-extras`
- **Build wheels**: `uv build --package bro` and `uv build --package bro-dev`
- **Discover usage/flags**: run any CLI or shell script with `--help`

## Conventions

Member pyprojects carry the tool config (ruff, pyright, deptry, pytest). The development style policy is `bro/bro/prompts/dev/style.md` (tool-served to dev sessions as `dev-style-source::read`); shell scripts follow `bro-dev/bro_dev/shell_policy.py` (prelude sourcing, shebang), enforced by each member's `shell_policy_test.py`.
