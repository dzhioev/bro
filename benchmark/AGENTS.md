# Terminal-Bench harness adapter

Driving a bro as an agent under Harbor, the harness Terminal-Bench runs on.
The distribution is
`bro-benchmark` and its package is `bro.benchmark`, a portion of the framework's `bro` namespace;
nothing in the framework imports it.
What the project is and how to drive it is `README.md`.

## Development

This is not a uv workspace member:
`harbor` reaches `openai < 3` through litellm while `bro-native`
pins `openai == 3`, so no single lock satisfies both.
The directory therefore locks and syncs on its
own — `uv sync --directory benchmark --all-groups` builds `.venv` from the `uv.lock` committed beside
this file — and depends on core and ride through editable path sources.
The ride dependency supplies
scoped-store materialization without pulling the native engine's other `openai` major;
the two must
never meet in one interpreter.
The relocatable bundle builds and installs `bro` plus `bro-native`
from the root workspace instead of adding the engine to this project's environment.

Formatting and linting stay the repository root's, which walks this directory through its
`[tool.ruff] src`.
The suite's environment rebuild is the root's too, but reaches a run only through
the conftest at its pytest root
— so the `conftest.py` beside this file applies it, and a suite that
did not would inherit the session running it.
pytest and pyright run from here instead, inside `.venv`:
`run-tests` syncs it and
drives both, and `--skip benchmark` skips that whole stage.
Build the wheel with `uv build` from this
directory rather than `uv build --package`.

The `*_e2e_test.py` modules stay out of the gate's roster:
they build a bundle and drive the host
docker daemon, the way `ride/ride/e2e_test.py` does.
The two that grade a trial spend real tokens as well, so naming the file is not enough
— they carry the repository's token opt-in and skip without it.
Run them explicitly, from `.venv`:

```
uv run --directory benchmark pytest bro/benchmark/bundle_e2e_test.py
BRO_LLM_TESTS=1 uv run --directory benchmark pytest bro/benchmark/harbor_e2e_test.py
BRO_LLM_TESTS=1 uv run --directory benchmark pytest bro/benchmark/benchmark_job_e2e_test.py
```

## Components

- `bro/benchmark/bundle.py` (`benchmark-bundle`) — builds the relocatable directory a foreign container runs `bro` from:
  a pinned standalone CPython, the dependencies `WHEEL_PACKAGES` resolves to against the workspace lock, those distributions themselves entering as built wheels, and a shim setting `PYTHONPATH` over them.
  Its manifest records those inputs and gives the bundle a content-derived identity.
  `Bundle` is the layout a consumer addresses — shim, interpreter, site-packages, CA store, manifest, and identity;
  `built(root)` reports an absent, incomplete, or malformed bundle rather than building one behind the caller's back
- `bro/benchmark/harbor_agent.py` — `BroAgent`, the `BaseInstalledAgent` harbor imports.
  Its Harbor version is the uploaded bundle's identity.
  `install()` uploads the bundle and a scoped store holding only the LLM key, then runs
  `bro show <bro>` through the uploaded bundle
  — the one validation the host cannot make, and a
  smoke test of the bundle in the task's own image.
  `run()` is a single
  `bro run <bro> <instruction>` under `setsid`, reaped through a fresh root exec when
  harbor cancels the phase.
  Harbor's `model_name` carries a registered provider's name plus a `--llm` recipe with its provider slot dropped
  — `<provider>/<model>[:<effort>][+fast]`, mapped onto `--llm :<recipe>`, the spelling
  that keeps the persona's own spec;
  the retry classification is the roster providers' declared failure signatures
- `bro/benchmark/harbor_environment.py` — `UnmountedDockerEnvironment`, the environment the job
  config names:
  it keeps a trial's logs, artifacts and reward inside the task container for harbor
  to copy out, so a job runs wherever the docker daemon is reachable and leaves nothing on the
  docker host
- `bro/benchmark/pricing.py` — the source-dated model price table and exact per-call calculator over `bro.llm.usage`'s four billed classes
- `bro/benchmark/trajectory.py` — converts the projected local trail in each finished trial into
  Harbor's ATIF v1.7 models at `agent/trajectory.json`, including billed-class counts and priced step/final metrics;
  `convert_job_trajectories()` is the post-run job-directory sweep,
  and `job_trajectory_cost_usd()` is the strict report-time repricing surface
- `bro/benchmark/job.py` (`bro.benchmark.job`) — runs Harbor against a known concrete job directory,
  then owns the ordered host-side post-run pipeline:
  trajectory conversion, an optional private or public Harbor Hub upload, then durable retention when configured
- `bro/benchmark/retention.py` — copies every file in a finished run to the configured S3 bucket,
  with the strict run-cost report, a content inventory, and the score-producing config and bundle identity in a manifest uploaded last
- `bro/benchmark/compare.py` (`bro.benchmark.compare`) — aggregates one local job or one complete retained-run cohort into per-task reward means,
  resolves a public leaderboard submission and its exact filtered Hub trials through a beside-the-runs cache, and marks task-level deltas against that reference
- `bro/benchmark/terminal_bench_2_1.yaml` — the pinned harbor job config, and with the bundle the
  whole of what a score depends on
