# Managed-session image assets

These assets ship in `bro-ride` and are consumed by `ride.workspace.build_context`. The shell prelude remains in the framework's `bro/setup/` directory and is resolved through `bro.shell`.

## Components

- `base_image/` — Dockerfile and builder for the local-only `bro-base` image
- `container/` — managed-session Dockerfile, entrypoint, clone/submodule helper, Claude Code version pin, maintenance script, and host-only entrypoint smoke test

The assembled context injects the container files and the framework shell helpers under `.bro-container/`. The image resolves dependencies from the operated project's complete manifest set, installs workspace members editably from the full source context, and stages the manifests beside the baked venv. At launch the entrypoint links that venv into a new clone; the repository's `setup.sh` re-syncs when its manifests diverge.

`bump-claude-code.sh` and `test_smoke.sh` are checkout-only and excluded from the wheel. The Dockerfiles, entrypoint, git helper, version pin, and base-image builder ship with `bro-ride`.
