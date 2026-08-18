# Terminal-Bench harness adapter

Driving a bro as an agent under Harbor, the harness Terminal-Bench runs on. The distribution is
`bro-benchmark` and its package is `bro.benchmark`, a portion of the framework's `bro` namespace;
nothing in the framework imports it. What the project is and how to drive it is `README.md`.

## Development

This is not a uv workspace member: `harbor` reaches `openai < 3` through litellm and the framework's
`agent` extra pins `openai == 3`, so no single lock satisfies both. The directory therefore locks and
syncs on its own — `uv sync --directory benchmark --all-groups` builds `.venv` from the `uv.lock`
committed beside this file — and depends on the framework through
`bro = { path = "..", editable = true }` with no extras. Keep it that way: the host side of this
project reads only the framework's base layer, never the class graph that pulls in the other
`openai` major, and the two must never meet in one interpreter.

Formatting and linting stay the repository root's, which walks this directory through its
`[tool.ruff] src`. pytest and pyright run from here instead, inside `.venv`: `run-tests` syncs it and
drives both, and `--skip benchmark` skips that whole stage. Build the wheel with `uv build` from this
directory rather than `uv build --package`.

The `*_e2e_test.py` modules stay out of the gate's roster: they build a bundle and drive the host
docker daemon, the way `ride/ride/e2e_test.py` does, and the harbor one spends real tokens. Run
them explicitly, from `.venv`:

```
uv run --directory benchmark pytest bro/benchmark/bundle_e2e_test.py
uv run --directory benchmark pytest bro/benchmark/harbor_e2e_test.py
```

## Components

- `bro/benchmark/bundle.py` (`benchmark-bundle`) — builds the relocatable directory a foreign
  container runs `bro` from: a pinned standalone CPython, `bro[agent]` resolved from the workspace
  lock with the framework entering as a built wheel so `bros/*` ships with it, and a shim setting
  `PYTHONPATH` over the two. `Bundle` is the layout a consumer addresses — shim, interpreter,
  site-packages, and the CA store to point `SSL_CERT_FILE` at; `built(root)` reports an absent or
  incomplete bundle rather than building one behind the caller's back
- `bro/benchmark/harbor_agent.py` — `BroAgent`, the `BaseInstalledAgent` harbor imports.
  `install()` uploads the bundle and a scoped store holding only the LLM key, then runs
  `bro show <bro>` through the uploaded bundle — the one validation the host cannot make, and a
  smoke test of the bundle in the task's own image. `run()` is a single
  `bro run <bro> <instruction> --in-place` under `setsid`, reaped through a fresh root exec when
  harbor cancels the phase. Harbor's `-m openai/<model>` maps onto `--llm :<model>`, the spelling
  that keeps the persona's own recipe
- `bro/benchmark/harbor_environment.py` — `UnmountedDockerEnvironment`, the environment the job
  config names: it keeps a trial's logs, artifacts and reward inside the task container for harbor
  to copy out, so a job runs wherever the docker daemon is reachable and leaves nothing on the
  docker host
- `bro/benchmark/terminal_bench_2_1.yaml` — the pinned harbor job config, and with the bundle the
  whole of what a score depends on
