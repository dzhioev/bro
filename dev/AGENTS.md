# Development tooling

For repositories built on the bro framework, distributed separately from the runtime. The distribution is `bro-dev` and its package is `bro.dev`, a portion of the framework's `bro` namespace; none of it is imported by framework users, and none of it is specific to this checkout — what only means something here lives in the `bro-local` member (`local/`).

## Development

This workspace member owns only `pyproject.toml` — its distribution metadata, dependencies, and what its wheel carries. Formatting, linting, typing, and the test roster are the repository root's (`AGENTS.md`, "Development"), which covers this directory too; build the wheel with `uv build --package bro-dev`.

## Components

- `bro/dev/sync_scripts.py` — discovers CLIs and regenerates the project scripts table plus each distribution's committed `_entrypoints.py` bridge
- `bro/dev/usage_report.py` — aggregates token-accounting footers over a git range
- `bro/dev/install.py` — provisions the checkout-keyed `/var/ride` runtime root on the host, then installs the framework's commit-footer hooks (`bro/workflow/commit_footer.py`) and the repo-local `git golc` alias into the current repository
- `bro/dev/git_golc.py` — backs the repo-local `git golc` view with per-commit output-token credits
- `bro/dev/shell_policy.py` — reusable shell-policy assertion over an explicit repository root
- `bro/dev/packaging_policy.py` — reusable packaging-policy assertion over an explicit repository root: builds every distribution the workspace declares, plus any project the caller names beside it, and fails on a test module inside a wheel
- `bros/analyst/` — the `analyst` persona and its machinery, registered through the `bro` entry-point group. It answers questions about recorded runs by folding the trail store rather than by recalling it, mounting the same file/shell toolset as `dev` (there is no trails toolset — `rewind` and `bro.trails.client` are reached through the shell) and declaring nothing beyond the session baseline, because reading trails needs no credential of its own. Its `spell::report-usage` drives the shipped `scripts/trails_usage.py` and commits the report it writes as `<date>–<slug>.md`, over a window and optional focus the caller states in prose; where reports land is the operated repo's `[tool.bro.analyst] reports`, since an installed package directory is site-packages and no location can be assumed; this checkout points it at `bros/analyst/reports/`, which sits inside a shipped module root and is therefore named in `wheel-exclude` so no wheel carries a written report
