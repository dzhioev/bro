# CLAUDE.md

Development tooling distributed separately from the bro runtime framework. The package is `bro_dev`; none of it is imported by framework users.

## Components

- `bro_dev/bro.py` — the `bro-dev` persona registered through the `bro` entry-point group
- `bro_dev/sync_scripts.py` — discovers CLIs and regenerates the project scripts table plus each distribution's committed `_entrypoints.py` bridge
- `bro_dev/claude_commit_footer.py` — emits per-commit token-accounting deltas and aggregates them for squash merges
- `bro_dev/usage_report.py` — aggregates token-accounting footers over a git range
- `bro_dev/git_golc.py` — backs the repo-local `git golc` view with per-commit output-token credits
- `bro_dev/shell_policy.py` — reusable shell-policy assertion over an explicit repository root
- `bro_dev/template.py` — unregistered skeleton for a new console-script module
- `bro_dev/hooks/post-commit` — copied into a repository's git hooks and invokes the registered footer command
