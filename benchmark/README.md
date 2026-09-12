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

A benchmark task runs in a foreign image that must not be modified,
and several carry no Python at all, so the agent brings its own.
`benchmark bundle` builds a relocatable directory holding a pinned standalone CPython, the framework distributions a bro runs from — `bro`, `bro-native` and `bro-dev`
— resolved from the framework's lock, and a `bro` shim over them:

```
uv run --project benchmark benchmark bundle
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

Build the bundle once, then start Harbor:

```
uv run --project benchmark benchmark bundle
uv run --project benchmark bro.benchmark.job -c benchmark/bro/benchmark/terminal_bench_2_1.yaml
```

`bro.benchmark.job` runs Harbor and nothing after it.
The completed job directory is Harbor's raw output plus one `bundle.json` copied from the bundle the trials ran, so later workflows can derive the source commit and framework revision without consulting the workspace.
Conversion, publication, and durable retention are separate operations rather than part of this command.

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

Managed sessions carry no docker socket;
from inside one, start the job through the session broker instead:

```
benchmark-job start -c benchmark/bro/benchmark/terminal_bench_2_1.yaml --detach
benchmark-job check <request-id>
```

The host runs `bro.benchmark.job` with its own Docker access through the `benchmark` broker kind from `bro-bench`, pointed at the workspace's config and at the command job's output directory.
`start` and `check` print the artifact ref of the raw run;
`artifact get <ref>` makes it readable, with the whole `<jobs_dir>` under `output/` beside `stdout`, `stderr`, and `status.json`.
`benchmark-run` builds and starts the same raw run, then prints `results <path>  artifact <ref>` before its short report.
The artifact store dies with the ride, so retain its ref before the ride ends when the run must become durable.

## Retaining a run

`benchmark retain` takes either the artifact ref printed by `benchmark-run` or a local Harbor job directory:

```
uv run --project benchmark benchmark retain sha256:<artifact-digest>
uv run --project benchmark benchmark retain jobs/<job-name>
```

A managed session running it needs both `aws` and `benchmark_retention` granted at launch.
The retention credential is a JSON object with `bucket` and `region` fields;
the AWS credential supplies the S3 identity.

Retention snapshots the source once and copies every raw job file without changing the artifact or local directory.
The immutable key is `runs/<started-at UTC date>/<Harbor job UUID>/`.
Every object put is conditional on absence and carries its SHA-256 checksum.
A restart skips an already-written object only when its stored checksum matches, while any collision is refused.
`retention.json` is written last as the completion marker, and an existing marker refuses a second retain of that job.

The format 3 marker records:

- `job`: `id`, `started_at`, `finished_at`, and Harbor's `n_retries`
- `config`, `score_config_sha256`, and `roster_sha256`: the resolved job config and its score and roster identities
- `dataset`: the one `name` and `ref` shared by the trials
- `bundle`: `source_commit` and the content-derived `framework_revision`
- `pricing`: each provider table's `as_of`, `source`, `sha256`, and vendor-vocabulary `rates` for the models it priced
- `total_cost_usd`: null when any trial could not be priced
- `trials`: one row per trial directory with `trial`, `task`, `harness`, `bro`, `model`, `llm`, `rewards`, `reward`, `error`, `started_at`, `finished_at`, `root_trail_id`, `llm_calls`, `tokens`, and `cost_usd`
- `files`: the copied files as ordered `path`, `sha256`, and `size` rows

The files under each trial remain Harbor's raw output, including its local trail store at `<trial>/agent/ride/trails/`.
Trajectories are produced only by publication and are not part of a raw run.

## Querying retained runs

`benchmark query` loads DuckDB's `httpfs` and `aws` extensions and reads every complete format 3 marker directly from S3:

```
uv run --project benchmark benchmark query 'SELECT * FROM runs ORDER BY started_at DESC'
uv run --project benchmark benchmark query --file report.sql
uv run --project benchmark benchmark query
```

The last form opens an interactive DuckDB SQL shell over the same views.
The extensions are fetched on first use, so that first query needs network access.
A managed session needs both `aws` and `benchmark_retention` granted at launch, the same as retention.

The `runs` view has one row per retained marker with its job times and retries, config, bundle source commit and revision, dataset, score and roster digests, total cost, and manifest key.
The `trials` view expands each marker's trial rows and joins those run dimensions to their agent dimensions, rewards, error, times, trail identity, token counts, and cost.
Its `store_prefix` is the trial's retained local-store prefix.

This query compares two source commits per task and scores an errored trial as zero:

```sql
WITH scores AS (
  SELECT
    task,
    avg(coalesce(reward, 0)) FILTER (WHERE source_commit = '<baseline-commit>')
      AS baseline_mean,
    avg(coalesce(reward, 0)) FILTER (WHERE source_commit = '<candidate-commit>')
      AS candidate_mean
  FROM trials
  WHERE source_commit IN ('<baseline-commit>', '<candidate-commit>')
  GROUP BY task
)
SELECT task, baseline_mean, candidate_mean, candidate_mean - baseline_mean AS delta
FROM scores
ORDER BY task;
```

To inspect one retained trail, first select its `store_prefix` and `root_trail_id`, then sync that prefix into the local-store layout and point `rewind` at its data home:

```
aws s3 sync s3://<retention-bucket>/<store-prefix> /tmp/benchmark-trial/agent/ride/trails
mkdir -p /tmp/empty-bro-store
BRO_STORE=/tmp/empty-bro-store XDG_DATA_HOME=/tmp/benchmark-trial/agent rewind show <root-trail-id>
```

## Publishing a retained run

Inspect the retained run after retention and before publication, then choose its Hub visibility explicitly:

```
uv run --project benchmark benchmark publish runs/<date>/<job-id> --private
uv run --project benchmark benchmark publish runs/<date>/<job-id> --public
```

A managed session running it needs `aws`, `benchmark_retention`, and `harbor` granted at launch.
Publication downloads and verifies the raw run in temporary scratch, converts each recorded trail into an ATIF trajectory, and fills the per-trial and job costs from the immutable manifest's captured rates.
It passes the scoped Harbor key only to `harbor upload`.
The raw retained files and `retention.json` never change.
After a successful upload, an immutable `publications/<published-at>.json` beside the manifest records the visibility, Hub URL, and publication time.
The command prints that Hub URL and the S3 URL of the publication record.

## Importing a run's trails

`benchmark import-trails` copies every trail the run's trials recorded into the trails registry, so they read from the configured store like any other trail:

```
uv run --project benchmark benchmark import-trails runs/<date>/<job-id>
```

A managed session running it needs `aws`, `benchmark_retention`, and `trails` with the administer permission granted at launch.
Each trial's store is downloaded and verified against the manifest, then imported whole, parents first, under the trail ids the run recorded;
a trial that recorded no trail is skipped.
The import restarts safely:
a trail the registry already holds identically answers as success, and a different trail under the same id is refused.
The command prints each trial with the trail ids it imported.

Following a run as it happens means reading the log where
it is being written:
`docker exec <task-container> tail -f /logs/agent/bro.log`.

The container gets exactly one credential, the LLM key named by the `llm_credential` kwarg
— use a
dedicated, budget-capped instance.
It sits in a container where an LLM has unrestricted shell and
internet, and the task instruction is third-party text the bro treats as its request.
