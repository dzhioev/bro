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

It lands in `var/benchmark/bundle` unless `--output` says otherwise.
Its `bundle.json` manifest identifies the source commit, exact framework wheels, dependency pins, interpreter, target, and shim that produced it.
The canonical manifest digest is the bundle identity Harbor records as `agent_info.version` for every trial.
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

Build the bundle once, then start the job through the post-run pipeline:

```
uv run --project benchmark benchmark-bundle
uv run --project benchmark bro.benchmark.job -c benchmark/bro/benchmark/terminal_bench_2_1.yaml
```

`bro.benchmark.job` runs Harbor, converts every recorded trial trail to `agent/trajectory.json`, and then runs the configured post-run operations against the finished concrete job directory.
It does not upload to the Harbor Hub by default.
Pass `--upload private` or `--upload public` to run the idempotent `harbor upload` sweep after conversion;
the command prints the Harbor Hub job link and records it in the job's `upload.json`.
A host operator authenticates with `HARBOR_API_KEY` or `harbor auth login` as Harbor normally does.

When the host resolves a `benchmark_retention` credential, every run is then copied to its S3 bucket regardless of the Hub setting.
The credential is a JSON object with the exact fields `bucket` and `region`;
AWS authentication comes from boto3's ambient credential chain.
Retention adds `retention.json`, whose config, bundle identity and source commit, source-priced run cost, optional Hub link, and file hashes make the run independently inspectable.
It uploads that manifest last, so its presence marks a complete retained run.
A host without the credential skips retention and still runs the benchmark.

The job config is the whole reproducibility contract
— dataset digest, the bros under test, the model, concurrency, attempt depth, and the retry policy
— so a run is described by that file plus the bundle.
The config repeats every trial five times;
`-k/--n-attempts` overrides that depth when a wave run needs fewer attempts.
To narrow a run to a subset of tasks, add a `task_names` list of globs under the dataset:
harbor's `--include-task-name` applies only to a dataset the command line itself names,
which would mean restating the pinned digest there.

The score lands in `<jobs_dir>/<job-name>/result.json` (`jobs/` unless `-o` says otherwise), under
`stats.evals`, one entry per agent and dataset:
`pass_at_k`, `reward_stats`, `exception_stats`,
`n_trials`, `n_errors`.
Each trial keeps its own directory beside it, with the bro's activity log
(`agent/bro.log`), per-model token counts (`agent/usage.json`) and the trail the run recorded
(`agent/ride/trails/`) as the run's record.

They are copied out of the container once the trial ends,
so a job runs wherever the docker daemon is reachable and leaves nothing of a trial on the docker host.

The trail is a local trails store under the trial's own `agent/` directory as its data home.
Reading one back takes a reader resolving the same way
— the local backend, at that data home:

```
mkdir -p /tmp/empty-bro-store
BRO_STORE=/tmp/empty-bro-store XDG_DATA_HOME=<trial>/agent rewind show <trail-id>
```

On a host that configures no `trails` credential of its own, `XDG_DATA_HOME` alone is enough.

Compare one of the run's agents with the content-pinned native Claude Code leaderboard reference:

```
uv run --project benchmark bro.benchmark.compare jobs/<job-name> --agent bro:terminal
```

The report prints every task in the run with both agents' mean reward and trial count and their signed delta,
and marks differences larger than `--divergence` or tasks missing from the reference.
Reference tasks outside the run are omitted,
so a wave report stays scoped to that wave.
It closes with task coverage, matching trial totals, task-mean rollups, and divergence counts.
Use `--reference <submission-json-path-or-url>` to select another Terminal-Bench 2.1 leaderboard submission.
The first fetch reads its `source_jobs` from the submission,
filters the Hub rows by the submission's exact agent, version, model, dataset, and materialized trial IDs,
and honors its disqualifications as reward zero.
Harbor currently requires `harbor auth login` or `HARBOR_API_KEY` to read those public rows.
Later reports use the exact cached submission and Hub rows from `references/` beside a local job,
unless `--refresh-reference` is set.

A retention bucket is also a run input:

```
uv run --project benchmark bro.benchmark.compare s3://<bucket> --agent bro:terminal \
  --score-config-sha256 sha256:<digest> --roster-sha256 sha256:<digest>
```

The command lists complete runs by their `runs/**/retention.json` markers and aggregates all runs in the selected cohort.
The digest flags can be omitted when the bucket contains only one cohort.
Reference records for this form are cached under the bucket's `references/` prefix.

Managed sessions carry no docker socket;
from inside one, start the job through the session broker instead:

```
benchmark-job start -c benchmark/bro/benchmark/terminal_bench_2_1.yaml --upload private --detach
benchmark-job check <request-id>
```

`benchmark-run` accepts the same `--upload none|private|public` choice.
`none` is the default on both session commands.
For an unattended upload, store the Harbor API key as the `harbor` credential kind and launch the session with `--grant harbor`;
the broker hydrates that bounded credential into the host job's `HARBOR_API_KEY`.

The host runs `bro.benchmark.job` with its own docker access
(the `benchmark` broker kind, `local/bro/local/benchmark_job.py`),
pointed at the workspace's own config and at the job's own directory rather than the checkout's
`jobs/`.
`start` and `check` print the artifact ref of the finished run and the Hub link when it was uploaded;
`artifact get <ref>` makes it readable, with the whole `<jobs_dir>` under `output/` beside the run's
`stdout`, `stderr`, and `status.json`.
The artifact store dies with the session;
a configured retention bucket is the durable copy, while a host without one must copy out anything that should outlive the session.

Following a run as it happens means reading the log where
it is being written:
`docker exec <task-container> tail -f /logs/agent/bro.log`.

The container gets exactly one credential, the LLM key named by the `llm_credential` kwarg
— use a
dedicated, budget-capped instance.
It sits in a container where an LLM has unrestricted shell and
internet, and the task instruction is third-party text the bro treats as its request.
