# Workspace contracts

The framework keeps the dependency-light workspace contracts that core services read without installing the managed runtime. Workspace creation, provisioning, containers, credential hydration, and teardown live in `ride.workspace`; `bro` never imports `ride`.

## Components

- `git.py` — `git_out` / `git_run`, `no_prompt_env`, ref resolution, and the private fetch refs used to avoid concurrent-resolution races
- `paths.py` — project-root and runtime-root resolution, checkout-keyed state paths, fixed in-container mount paths, workspace naming, venv environment helpers, and `RuntimeLocationError`
- `project.py` — validated `[tool.bro]` project configuration and per-bro project sections
- `banner.py` — typed session facts plus visual and LLM renderings, consumed by launch surfaces and the `bro::banner` service tool
- `session.py` — in-session termination through `RIDE_RUNNER_PID`

Consumers import submodules directly; this package is not a re-export hub.
