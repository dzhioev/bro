# Workspace contracts

The framework keeps the dependency-light workspace contracts that core services read without installing the managed runtime.
Workspace creation, provisioning, containers, credential hydration, and teardown live in `ride.workspace`;
`bro` never imports `ride`.

## Components

- `git.py` — `git_out` / `git_run`, `no_prompt_env`, ref resolution, and the private fetch refs used to avoid concurrent-resolution races
- `paths.py` — explicit repository-root resolution, the flat global runtime paths, fixed in-container mount paths, workspace naming, venv environment helpers, and `RuntimeLocationError`
- `installation.py` — the environment's installation-home marker and home-name grammar
- `settings.py` — the installed home's packaged launch settings
- `project.py` — attached-tree build-context configuration and uninterpreted project sub-tables
- `banner.py` — typed session facts plus visual and LLM renderings, consumed by launch surfaces and the `bro::banner` service tool
- `session.py` — in-session termination through `RIDE_RUNNER_PID`, carrying the status the session is to report
- `human.py` — the human a session works for: the environment carrying their git identity, the checkout read that resolves it, and the session read back

Consumers import submodules directly;
this package is not a re-export hub.
