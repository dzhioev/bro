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
and several carry neither Python nor a CA store, so the agent brings its own.
`benchmark bundle` builds a relocatable directory in the materialized-runtime layout `ride --runtime-bundle` takes:
`venv/`, a pinned standalone CPython with the framework distributions a ride runs from
— `bro`, `bro-native`, `bro-dev` and `bro-ride`, resolved from the framework's lock
— installed into its own site-packages, every console script relocated to find that interpreter beside itself;
`bin/`, the shim farm over the session commands, which leads a session's `PATH` and keeps the bundled `python3` off it;
and `claude/`, the standalone Claude Code binary at the version ride pins, verified against the release manifest's checksum:

```
uv run --project benchmark benchmark bundle
```

It lands in `var/benchmark/bundle` unless `--output` says otherwise, and the downloaded binary is kept under `var/benchmark/claude-code` for the next build.
Its `bundle.json` manifest identifies the source commit, exact framework wheels, dependency pins, interpreter, target, and Claude Code binary that produced it.
The canonical manifest digest is the bundle identity Harbor records as `agent_info.version` for every trial.
Copying the directory somewhere is the whole installation:

```
docker exec <container> mkdir --parents /installed-agent
docker cp var/benchmark/bundle <container>:/installed-agent/bro
docker exec <container> /installed-agent/bro/venv/bin/bro show terminal
```

The bundle targets linux/x86_64 glibc, and a build refuses any other host rather than producing one
that will not run.

## Running a benchmark

Harbor drives every task container through the `docker compose` CLI plugin, which nothing else in
this repository needs — install it before the first run.

A run is named by presets committed under `benchmark/`, a JSON file each:

- `agents/<name>.json` — the Harbor `agents` list: the bro, the harness driving it, the model, and the credential the container gets, named `<bro>-<harness>-<model>-<effort>` after them
- `settings/<name>.json` — everything else Harbor takes, so attempt depth, concurrency, the retry policy, and the environment:
  `baseline` for a score, `smoke` for a cheap check
- `datasets/<name>.json` — a dataset pin: the registry `name`, the content `ref`, and the `task_namespace` Harbor qualifies its task names with.
  A pin is immutable by convention: a new revision of a dataset is a new file, never an edit of the ref in place, so every task set and every retained run keeps meaning what it meant
- `tasks/<name>.json` — a task set: a dataset pin by name and a list of bare task names, empty for the whole dataset

The committed task sets over `terminal-bench-2-1` come in families, named with the family as prefix so a `tasks_preset LIKE 'category-%'` query selects a whole family of retained runs:

- `category-<name>` — eight sets partitioning the dataset by the task manifests' own category label.
  `software-engineering`, `system-administration`, `security`, and `debugging` hold the tasks under those labels, `software-engineering` taking the `optimization` task as well;
  `science` holds `scientific-computing` and `mathematics`;
  `data` holds `data-science`, `data-processing`, `data-querying`, and `personal-assistant`;
  `ml` holds `machine-learning` and `model-training`;
  and `files-and-media` holds `file-operations`, `video-processing`, and `games`
- `difficulty-<tier>` — three sets partitioning the dataset by the task manifests' own difficulty metadata:
  a task the authors label hard is `hard`;
  one they label easy, or medium with an expert time estimate of thirty minutes or less, is `easy`;
  the rest is `medium`.
  The rule is a first cut from metadata, to be replaced by tiers from measured pass rates once a whole-dataset baseline exists
- `core` — twenty tasks for day-to-day runs: every category set represented, seven easy, eight medium, and five hard by the rule above, each with an agent timeout of thirty minutes or less, and `smoke` among them.
  It leaves out large model and dataset downloads, the task tagged `no-verified-solution`, and the hard tasks the public leaderboard shows as essentially unsolved, since neither a regression nor an improvement would show on those
- `long-horizon` — the sixteen tasks with an agent timeout above thirty minutes, the ones `core` leaves out, for context compaction and long-run stability
- `smoke` — three quick tasks for checking that a run works at all

`benchmark-run` composes one of each into the Harbor job config, writes it to `var/benchmark/runs/<id>.json`, rebuilds the bundle unless `--keep-bundle`, runs the job, and prints where the results landed before a short report:

```
benchmark-run --agents terminal-bro-gpt-5.6-terra-high --settings baseline --tasks smoke
```

`--dataset <name>` stands in for `--tasks` for a whole-dataset run, and positional task names beside it, bare or Harbor-qualified, make an ad hoc selection:

```
benchmark-run --agents terminal-bro-gpt-5.6-terra-high --settings smoke --dataset terminal-bench-2-1 regex-log
```

The composed config is what runs, gets digested, and lands in the retention manifest, so a run's identity is its three preset names in git
— a baseline and the runs compared against it are the same names rather than the same retyped command line.
Terminal-Bench 2.2 would arrive as a new pin file and new task sets naming it;
task names are names inside one revision, so a 2.1 list is not reused against 2.2 without checking.

Where the job runs depends on where the command runs.
A managed session carries no docker socket and needs `:launch.benchmark`, whose worker payload has no fields, so from inside one the job goes through the session broker:
the host runs `bro.benchmark.job` with its own Docker access through the registered `benchmark` worker type from `bro-bench`, pointed at the composed config,
and the finished run comes back as an artifact whose `output/` holds the job directory beside `stdout`, `stderr`, and `status.json`.
`benchmark-run` prints `results <path>  artifact <ref>`;
`artifact get <ref>` makes the run readable again, and the artifact store dies with the ride, so retain the ref before the ride ends when the run must become durable.
`benchmark-job start -c <config> --detach` and `benchmark-job check <request-id>` drive the same worker type through `launch`, for a composed config already in the tree.
Outside a session, Harbor runs right here into `jobs/<id>` (`--jobs-dir` moves it), the same command from the benchmark project's own environment:

```
uv run --project benchmark benchmark-run --agents terminal-bro-gpt-5.6-terra-high --settings baseline --tasks smoke --retain
```

`--retain` finishes with `benchmark retain` on the run, from the credentials the process holds.
A retention failure fails the command with the job left where it is, and `benchmark retain <job directory>` finishes it.

`bro.benchmark.job` runs Harbor and nothing after it.
The completed job directory is Harbor's raw output plus `bundle.json`, copied from the bundle the trials ran, and `presets.json`, the names the config was composed from,
so later workflows derive the source commit, framework revision, and cohort without consulting the workspace.
Conversion, publication, and durable retention are separate operations rather than part of this command.

Each trial is one `ride solo` inside the task container:
an unboxed root in the task's own directory, on the uploaded bundle as its runtime, permitted to have its summons join its party and start nothing
— so the `terminal` bro delegates to further `terminal`s as processes beside it in the same tree, with no Docker in the container.
The launch declares the container a sandbox (`--env IS_SANDBOX=1`):
its agent phase runs as root, and that declaration is what lets claude skip permission prompts under uid 0.
The agent preset's `harness` kwarg selects the driving loop, `bro` or `claude`, and `llm_credential` the one credential the container gets:
the LLM key on the bro harness, the `claude_code` setup token on the claude one.
The committed agent presets cover both harnesses, and the developer bro as it ships beside the purpose-built one, so their difference is a measured number.

The score lands in the job directory's `result.json`, under
`stats.evals`, one entry per agent and dataset:
`pass_at_k`, `reward_stats`, `exception_stats`,
`n_trials`, `n_errors`.
Each trial keeps its own directory beside it, with the ride's activity log
(`agent/bro.log`) and its runtime root (`agent/ride/`) as the run's record:
the trail store (`agent/ride/trails/`), the workspace records, and the launch audit.

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

## Retaining a run

`benchmark retain` takes either the artifact ref or the local job directory `benchmark-run` printed:

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

The format 4 marker records:

- `job`: `id`, `started_at`, `finished_at`, and Harbor's `n_retries`
- `config`, `score_config_sha256`, and `roster_sha256`: the resolved job config and its score and roster identities
- `presets`: the `agents`, `settings`, and `tasks` preset names the config was composed from, `tasks` null for an ad hoc selection
- `dataset`: the one `name` and `ref` shared by the trials
- `bundle`: `source_commit` and the content-derived `framework_revision`
- `pricing`: each provider table's `as_of`, `source`, `sha256`, and vendor-vocabulary `rates` for the models it priced
- `total_cost_usd`: null when any trial could not be priced
- `trials`: one row per trial directory with `trial`, `task`, `harness`, `bro`, `model`, `llm`, `rewards`, `reward`, `error`, `started_at`, `finished_at`, `root_trail_id`, `llm_calls`, `tokens`, and `cost_usd`
- `files`: the copied files as ordered `path`, `sha256`, and `size` rows

The files under each trial remain Harbor's raw output, including its local trail store at `<trial>/agent/ride/trails/`.
Trajectories are produced only by publication and are not part of a raw run.

## Querying retained runs

`benchmark query` loads DuckDB's `httpfs` and `aws` extensions and reads every complete format 4 marker directly from S3:

```
uv run --project benchmark benchmark query 'SELECT * FROM runs ORDER BY started_at DESC'
uv run --project benchmark benchmark query --file report.sql
uv run --project benchmark benchmark query
```

The last form opens an interactive DuckDB SQL shell over the same views.
The extensions are fetched on first use, so that first query needs network access.
A managed session needs both `aws` and `benchmark_retention` granted at launch, the same as retention.

The `runs` view has one row per retained marker with its job times and retries, config, bundle source commit and revision, dataset, score and roster digests, preset names, total cost, and manifest key.
The `trials` view expands each marker's trial rows and joins those run dimensions to their agent dimensions, rewards, error, times, trail identity, token counts, and cost.
Its `store_prefix` is the trial's retained local-store prefix.

A cohort is selected by its preset names (`agents_preset`, `settings_preset`, `tasks_preset`) rather than by digest.
This query compares two source commits of one cohort per task and scores an errored trial as zero:

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
    AND agents_preset = 'terminal-bro-gpt-5.6-terra-high'
    AND settings_preset = 'baseline'
    AND tasks_preset = '<task-set>'
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

The trial store is rooted at the one credential the `llm_credential` kwarg names and includes any stored names its preserved `$cred` references reach.
The uploaded bundle's own framework reads the host store's default pick and hydrates that store, so the code that writes it is the code inside the bundle that reads it:
on the bro harness an LLM key
— use a dedicated, budget-capped instance
— and on the claude harness a `claude_code` setup token, an account credential no budget caps, so a public run uses a dedicated Claude account whose token is revoked after the run.
The ride hydrates it into a private temporary store outside the collected tree.
It sits in a container where an LLM has unrestricted shell and
internet, and the task instruction is third-party text the bro treats as its request.
