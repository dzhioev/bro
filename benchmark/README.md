# bro-benchmark

`bro-benchmark` runs a bro as an agent under [Harbor](https://github.com/harbor-framework/harbor),
the harness that distributes and executes Terminal-Bench.
It ships as `bro.benchmark`, a portion of
the framework's `bro` namespace package, from a project of its own beside the framework workspace.

Sync its environment before using it
— the repository's `./setup.sh` leaves it alone:

```
uv sync --directory benchmark --all-groups
```

## The bundle

A benchmark task runs in a foreign image that must not be modified, and several carry no Python at
all, so the agent brings its own.
`benchmark-bundle` builds a relocatable directory holding a pinned
standalone CPython, the framework distributions a bro runs from — `bro`, `bro-native` and `bro-dev`
— resolved from the framework's lock, and a `bro` shim over them:

```
uv run --project benchmark benchmark-bundle
```

It lands in `var/benchmark/bundle` unless `--output` says otherwise, and rebuilding it from one
commit reproduces the same contents.
Copying the directory somewhere is the whole installation, and
the shim inside it is the framework's `bro` command:

```
docker cp var/benchmark/bundle <container>:/installed-agent/bro
docker exec <container> /installed-agent/bro/bro show terminal
```

The bundle targets linux/x86_64 glibc, and a build refuses any other host rather than producing one
that will not run.

## Scoring Terminal-Bench 2.1

Harbor drives every task container through the `docker compose` CLI plugin, which nothing else in
this repository needs — install it before the first run.

Build the bundle once, then start the job:

```
uv run --project benchmark benchmark-bundle
uv run --project benchmark harbor job start -c benchmark/bro/benchmark/terminal_bench_2_1.yaml
```

The job config is the whole reproducibility contract
— dataset revision, the bros under test, the
model, concurrency, and the retry policy
— so a run is described by that file plus the bundle.
`-k/--n-attempts` repeats each trial.
To narrow a run to a subset of tasks, add a `task_names` list
of globs under the dataset:
harbor's `--include-task-name` applies only to a dataset the command
line itself names, which would mean restating the pinned revision there.

The score lands in `<jobs_dir>/<job-name>/result.json` (`jobs/` unless `-o` says otherwise), under
`stats.evals`, one entry per agent and dataset:
`pass_at_k`, `reward_stats`, `exception_stats`,
`n_trials`, `n_errors`.
Each trial keeps its own directory beside it, with the bro's activity log
(`agent/bro.log`) and per-model token counts (`agent/usage.json`) as the run's record.

Both are copied out of the container once the trial ends, so a job runs wherever the docker daemon
is reachable and leaves nothing of a trial on the docker host.

Managed sessions carry no docker socket;
from inside one, start the job through the session broker instead:

```
benchmark-job start -c benchmark/bro/benchmark/terminal_bench_2_1.yaml --detach
benchmark-job check <request-id>
```

The host runs the same harbor command with its own docker access
(the `benchmark` broker kind, `local/bro/local/benchmark_job.py`),
pointed at the workspace's own config and at the job's own directory rather than the checkout's
`jobs/`.
`start` and `check` print the artifact ref of the finished run;
`artifact get <ref>` makes it readable, with the whole `<jobs_dir>` under `output/` beside the run's
`stdout`, `stderr`, and `status.json`.
The store dies with the session, so copy out whatever should outlive it.

Following a run as it happens means reading the log where
it is being written:
`docker exec <task-container> tail -f /logs/agent/bro.log`.

The container gets exactly one credential, the LLM key named by the `llm_credential` kwarg
— use a
dedicated, budget-capped instance.
It sits in a container where an LLM has unrestricted shell and
internet, and the task instruction is third-party text the bro treats as its request.
