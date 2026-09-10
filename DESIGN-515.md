# Design under review: restructure the benchmark pipeline around retained runs

The `## Design` section of https://github.com/dzhioev/bro/issues/515 as the review-and-plan phase proposes to finalize it.
This file exists for the review alone and never lands;
the settled text goes back onto the task.

## Design

**Commands.**
`bro.benchmark.job` stays the host entry the `benchmark` broker kind runs, and it runs Harbor and nothing else.
Every post-run step is a verb of one `benchmark` command in the benchmark project, runnable from a session or a host shell alike:
`benchmark bundle` (today's `benchmark-bundle`), `benchmark retain`, `benchmark publish`, `benchmark query`, and `benchmark import-trails`.
From a session the command runs through `uv run --project benchmark`, with the credentials it reads granted at launch:
`aws` and `benchmark_retention` for retain, query and import-trails, `harbor` for publish, and `trails` with the administer permission for import-trails.
Launch scoping hydrates a session's store from the kinds the outer `ride` installation registers, and that installation cannot carry `bro-benchmark`, whose Harbor lock conflicts with the workspace's.
So the benchmark's launcher-side half becomes a workspace member of its own, `bench/`, publishing `bro-bench` (package `bro.bench`):
the `harbor` and `benchmark_retention` credential kinds, the `benchmark` broker kind, and the `benchmark-job` and `benchmark-run` session commands, moved out of `bro-local`.
The root environment installs it as a member, so launch scoping knows the kinds, and `bro-benchmark` depends on it, so the same registry serves the commands in their own venv.

**Job.**
The `benchmark` broker kind's result is the raw job directory as a session artifact, and `benchmark-run` prints the artifact ref beside the report it renders from that directory.
`--upload` leaves the kind's args, `benchmark-job start`, and `benchmark-run`.
Once Harbor returns, `bro.benchmark.job` copies the bundle's `bundle.json` into the job directory, so the run carries the provenance of the bundle its trials ran, written by the process that ran them.
A post-run verb takes an artifact ref, resolved through `artifact get` to the run directory holding the one job under `output/`, or a local job directory path.
The artifact view is read-only, so no verb writes into its source.

**Retention.**
`benchmark retain <ref|path>` copies Harbor's output verbatim under `runs/<YYYY-MM-DD>/<job-id>/<relative path>` and puts `retention.json` last as the completion marker;
the date is the job's `started_at` in UTC and the id is Harbor's job UUID.
Nothing ever updates an object in place:
the inventory is taken once before the first put, and every put is conditional on its key being absent and carries the file's SHA-256 checksum;
a retry skips a key whose stored checksum equals the inventory's and refuses any other, and the marker goes last under the same condition, so a run whose marker exists is refused, not merged.
No trajectory is retained.
Retain reads the source commit and the bundle identity from the job's `bundle.json`, checks every trial that reached the agent's install against it through `agent_info.version`, and reads no workspace bundle;
a trial that failed before install has no agent files and still gets its error row.
A job whose trials span more than one dataset (`TrialResult.source`) is refused, since the manifest names one.
The manifest, format 3:

- `job`: `id`, `started_at`, `finished_at`, `n_retries`
- `config`: the job config Harbor resolved
- `score_config_sha256`, `roster_sha256`
- `dataset`: `name`, `ref`
- `bundle`: `source_commit`, `framework_revision`
- `pricing`: per provider that priced a call, the table's `as_of`, `source`, and `sha256`, and the rates of the models the run used, in the vendor's vocabulary
- `total_cost_usd` (null when any trial is unpriced)
- `trials[]`, one row per trial directory:
  `trial`, `task`, `harness`, `bro`, `model`, `llm`, `rewards` (the verifier's map, null on error), `reward` (its `reward` entry, the dataset's primary metric), `error` (null on success), `started_at`, `finished_at`, `root_trail_id`,
  `llm_calls`, `tokens` (`input`, `cache_write`, `cache_read`, `output`), `cost_usd` (null when unpriced)
- `files[]`: `path`, `sha256`, `size`

`trial` is the directory name, Harbor's identity for one attempt;
Harbor records no attempt index and rewrites a retried trial in place, so neither is a column, and the job's retry count is the one Harbor keeps.
`task`, `rewards`, `error`, and the timestamps come from the trial's `result.json`;
`harness`, `bro`, and `model` come from the trial's `config.json`, the identity the job requested, so an error row without a trail still carries every dimension:
the agent's `harness` and `bro` kwargs and its `model_name`, `harness` being `bro` where the config names none, the agent's own default;
`llm` (the recipe the run resolved) and `root_trail_id` come from the trail header, whose `harness` and `bro` must agree with the config;
`llm_calls`, `tokens`, and `cost_usd` sum every trail in the trial's store.
A trial that recorded no trail has `root_trail_id`, `llm`, `tokens`, and `cost_usd` null and `llm_calls` zero.
A trial's retained store sits at `<run prefix>/<trial>/agent/ride/trails/`;
that path is derived, never stored.
Headers carry no benchmark fields.

**Pricing.**
Prices are provider facts.
Each provider module under `bro/llm/llms/` declares its price table in the vendor's own vocabulary, with source URL and as-of date beside the numbers, and prices one call from the raw usage record a trail step carries;
`bro.llm.providers` exposes `price(provider, model, usage, service_tier, table=None)` the way it exposes `failure_signatures`, pricing against the provider's current table unless the caller hands it one.
The provider is the trail header's `native.llm.type`;
the model and the usage are the projected `llm_call` message's, so a Claude harness trail prices the transcript's model slug and never the header's `fable` alias;
the service tier is the response's, which the bro harness projection adds to `llm_call` when the response carries one, since OpenAI's priority tier bills its own rates.
`bro.trails.cost` sums one trail's priced calls over a `TrailsStore`;
an unpriced model yields `None`, and a strict variant raises for surfaces that must not report a partial number.
A Claude harness cost is the Anthropic list price, an imputed number for a session billed to a subscription token.
Pricing runs after the run, over the trail:
retention prices with the current tables and records the rates it used in the manifest's `pricing`, and publish prices from those recorded rates;
the two agree by construction, and a later table change never touches a retained run.
No cost code lives in the Harbor agent, and `benchmark/bro/benchmark/pricing.py` goes.
This reverses #356's decision to keep prices out of the framework, taken when the benchmark was the only consumer.
The four normalized classes stay for reporting (usage file, commit footer, analyst report), where a provider-neutral count is the point.

**Publish.**
`benchmark publish <run prefix> --private|--public` downloads the retained run into scratch, converts each trial's trails to an ATIF trajectory, and fills `cost_usd` into the trial and job `result.json` copies from the manifest's rates.
It then runs `harbor upload` and puts `publications/<published_at>.json` (`visibility`, `url`, `published_at`) beside the manifest, append-only so a private publish followed by a public one rewrites nothing.
The #355 inspection sits between retain and publish;
the command documents that order rather than enforcing it.
The retained copy is Harbor's output;
the published copy is that plus trajectories plus cost.
Harbor's uploader reads `result.json` and `config.json` from the directory and inserts the trial's `agent_result.cost_usd`, so the filled copies are what the Hub shows.
A summoned child whose `summoned_by` names no `step_id`, the shape a summon from a Claude session or from the `summon` CLI records, attaches at the trajectory root rather than failing the trial;
#523 is the general fix.

**Query.**
No index and no database.
`duckdb` joins the benchmark project's dependencies.
`benchmark query` opens a connection with the `httpfs` and `aws` extensions, which DuckDB fetches on first use.
It authenticates through `CREATE SECRET (TYPE s3, PROVIDER credential_chain)` in the retention credential's region, so the `aws` install hook's shared-credentials file serves it, and defines two views over `s3://<bucket>/runs/*/*/retention.json`:
`runs`, one row per manifest with `format = 3`, and `trials`, one row per trial row joined with its run's identity (job id, started date, source commit, framework revision, dataset, both digests, the manifest's key) plus the derived store prefix.
The views declare their column types (`config` and `rewards` as JSON) rather than auto-detecting them, so a manifest's shape cannot change a column.
It runs a SQL file or an inline statement, or drops into DuckDB's shell.
Baseline versus candidate is one query grouped by task, filtered by two commits, scoring an errored trial as zero the way the leaderboard does (`coalesce(reward, 0)`);
the README carries it.
`bro.benchmark.compare` and its tests go;
#423's leaderboard reference becomes a DuckDB source over the submission JSON when it is wanted.
An index becomes a projection of the manifests if it is ever needed, so nothing here is thrown away.

**Trails.**
The interchange format is the local store layout:
`trails/<id>/{header.json, steps.jsonl, context.json}` plus the content-addressed `trails/tools/<sha256>.json` blobs.
A trail header and each of its rows carry `format`, `bro.trails.model.TRAIL_FORMAT` stamped by every backend at the write and read as 1 where absent;
every reader of the layout, import, retention, and the ATIF converter among them, accepts the formats it knows, upgrading each row in memory from the format it names through the declared per-format steps, and refuses a newer one by name.
A store writes rows in its own format only, so a trail is readable in any physical state, and the header's `format` is the floor every row of the trail reaches.
A stored trail keeps its formats until a writer reopens it:
an append or attach onto a trail whose header names an older format first migrates its rows chunk by chunk, each rewrite conditional on the row's stored format, so a lost race is a skip and a killed migration resumes on the next write;
it then stamps the header conditionally on its old format, then writes;
a concurrent append meanwhile lands current-format rows, which the migration never touches.
So a recorder crosses a format-bumping deploy with its open trail intact, a fork edge still marks a real fork, and nothing needs a whole-trail lock that Dynamo does not have.
An admin operation applying the same migration is how an operator upgrades a sealed trail, and nothing here requires it.
A read of a tool blob by digest joins the `TrailsStore` contract and the server routes.
`trails export <id>...` writes the named trails and every ancestor reachable by id through `forked_from` and `summoned_by` into that layout through the read contract;
the summon closure downward is not walkable through it, since the contract has no `summoned_by` selector and Dynamo no index over it (#524), so summoned children are named explicitly.
`trails import <dir>` goes through `import_trail`, a new `TrailsStore` method on both backends and an admin route under the administer permission, with `NetworkStore` forwarding it.
It writes the header as recorded, every field it holds, with only the server-derived aggregate (`SERVER_DERIVED_NATIVE_FIELDS`) cleared and refolded from the rows the way recompute does;
the minted `lineage_head` cuts are kept, since no row derives them;
the rows go verbatim through the store's own row writer, so `payload_sha256` survives and Dynamo's body spill applies, and a row the destination's adapter refuses fails the import rather than being skipped;
every tool blob a row's `tools_sha256` names is in the directory or already at the destination, verified against its digest and stored before the trail is sealed;
a parent is required before its children.
The wire is begin, chunk, seal:
begin creates the header unsealed and answers an unsealed trail with the same header as itself;
a chunk appends at an offset and answers an identical chunk already present as a no-op the way `append_records` does;
seal writes the recorded `end` and refolds;
a sealed trail with the same id is a collision unless its header and rows are identical, which answers as success, so `benchmark import-trails` restarts over a partly imported run.
`benchmark import-trails <run prefix>` downloads each trial's store and imports it whole, parents first.

**Agent under test.**
The trial runs `ride` inside the task container with `--harness` selectable:
#131's process-host mode.
The `--in-place` inner contract exists (`ride solo|along --in-place --workspace … --harness … <bro> <prompt>`), and it only consumes a provisioned `BROKER_CHANNEL`;
what is missing is the outer around it inside a foreign container:
a broker root started there (`ride` refuses a container launch on the `/.dockerenv` probe today), whose summon lowering spawns supervised processes sharing the container's workspace with child stores derived from the container's own store,
where `run_root_via_broker` lowers every summon to a Docker child today;
the session records under `/logs/agent`;
the uploaded bundle standing in for the runtime volume with `bro-ride` added to its wheel roster;
and for the Claude harness Node, the CLI, and the `claude_code` token, with the #355 exposure review redone for the second credential.
The `terminal` bro withholds Claude's native delegation tools on the claude harness (`bro.harness.claude.DELEGATION`), since the recorder records no subagent sidecar and a trial's totals come from its trails;
#528 records sidecars as trails.
Until #131 lands, every trial row's `harness` is `bro`.

**Rollout.**
The trails server is a separately deployed process:
it deploys before any client imports, an old client is unaffected by the added routes, and a new client against an old server reports the missing route from the plain 404 it gets.
The `benchmark` kind's handler runs from the operator's frozen runtime bundle and invokes the workspace tree's `bro.benchmark.job`;
the installation is re-synced after the integration lands, since a handler from before it would pass `--upload` to a job entry that no longer takes it, and fails loudly there.
The retained trail directory is written by the task bundle's revision and read later by whichever revision runs publish or import;
the trails' own `format` carries that skew, and the manifest's `format` guards the query views.
