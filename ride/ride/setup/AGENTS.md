# Managed-session image assets

These assets ship in `bro-ride` and are consumed by `ride.workspace.build_context`. The shell prelude remains in the framework's `bro/setup/` directory and is resolved through `bro.shell`.

## Components

- `container/Dockerfile` — the shared runtime image: platform tools, pinned Claude Code, the ride user, plugin seed, entrypoint, and shell helpers.
- `container/project.Dockerfile` — the optional project dependency bake layered on the runtime image.
- `container/entrypoint.sh` and `git.sh` — session setup and repository clone/submodule handling.

The runtime image contains no Python distribution from the ride installation. A frozen runtime bundle is materialized into a named volume and mounted read-only at `/var/ride/runtime`; an optional project image contributes `/opt/project-venv` and its staged manifest set. `bump-claude-code.sh` and `test_smoke.sh` are checkout-only and excluded from the wheel.
