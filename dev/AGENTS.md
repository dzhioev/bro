# Development domain

Development tooling for repositories built on the bro framework, distributed separately from the core.
The distribution is `bro-dev`;
it contributes portions of the `bro` and `bros` namespaces and depends on `bro`.
Core never imports it.
Nothing here is specific to this checkout
— checkout-only policy and the `bro-dev` persona live in the `bro-local` member (`local/`).

## Development

This workspace member owns `pyproject.toml`
— its distribution metadata, dependencies, console scripts, persona entry points, and wheel roots.
Formatting, linting, typing, and the test roster are the repository root's (`AGENTS.md`, "Development"), which covers this directory too;
build the wheel with `uv build --package bro-dev`.

## Components

- `bro/dev/` — reusable repository-development utilities:
  - `sync_scripts.py` discovers CLIs and regenerates a distribution's scripts table plus committed `_entrypoints.py` bridge
  - `usage_report.py` aggregates token-accounting footers over a git range
  - `install.py` installs the commit-footer hooks and the repo-local `git golc` alias
  - `git_golc.py` backs that alias with per-commit output-token credits
  - `shell_policy.py`, `packaging_policy.py`, and `markdown_policy.py` expose reusable repository checks over an explicit root;
    the last also backs `check-markdown`, which holds a bulk prose reflow to whitespace and nothing else
  - `references.py` declares the `dev-style` source over `bro/prompts/dev/style.md`
- `bro/workflow/` — development delivery mechanics:
  co-author and token-accounting commit metadata,
  branch folding,
  PR landing,
  and the packaged git hooks.
  Its public import paths remain `bro.workflow.*`;
  its CLIs are `commit-footer`, `fold-branch`, and `land-pr`
- `bro/extra/github/poll_pr.py` — the `poll-pr` review watcher.
  The GitHub client and App-auth source it consumes remain in core at `bro.extra.github.api` and `.app`
- `bro/prompts/dev/style.md` — the development style policy, tool-served by the Dev persona through `dev-style-source::read`
- `bros/dev/` — generic developer with the file/shell/search toolset and the audit, credential-wiring, task, PR, and landing spells.
  Its optional `brog` feature mounts tracker tooling;
  its provisioning declaration installs the commit hooks
- `bros/lead/` — coordinator persona and `run-feature` spell
- `bros/terminal/` — standalone container developer, directly derived from `BaseBro`
- `bros/analyst/` — trail-analysis persona and machinery.
  It mounts the Dev toolset and declares the same commit-hook provisioning;
  its report destination comes from the operated repository's `[tool.bro.analyst] reports`

The `bro`, `bro.extra`, `bro.prompts`, and `bros` package trees are shared with other distributions.
Declare every shipped leaf in `[tool.uv.build-backend].module-name`;
do not build one of those parent trees wholesale.
