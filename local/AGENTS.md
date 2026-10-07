# Framework checkout integration

Checkout-specific personas, policy tests, and the repository test gate.
The workspace member publishes `bro-local`, contributing `bro.local` plus the `bro-dev` and `bro-eyebro` persona declarations used to work on this repository.
Nothing here belongs in a consumer installation.

## Development

This member depends on core and the generic development distribution.
Formatting, linting, typing, tests, and packaging use the root repository gate.
Run `sync-scripts --project local` after adding or removing a CLI, and build the wheel with `uv build --package bro-local`.

## Components

- `bros/` — this checkout's developer and reviewer persona declarations, and `bro-watch-probe`, the bare bro with a shell the conformance probes launch.
- `bro/local/prompts.py` — framework-project context shared by those personas.
- `bro/local/run_tests.py` (`run-tests`) — the checkout-wide staged test gate and explicit test rosters.
- `bro/local/gate_display.py` — how the gate reports: plain lines on a pipe, a live table on a terminal.
- `bro/local/green_trees.py` — the gate stages a tree has passed, kept as git notes on the tree object under `refs/notes/run-tests`.
- `bro/local/*_policy_test.py` — repository-wide policy assertions that have no consumer-neutral package owner.

## Test gate

`run-tests` (`bro/local/run_tests.py`) is the gate the root `AGENTS.md` names, for every member at once:

- `run-tests` — the test gate, a sequence of named stages:
  `lint` (console-script drift, deptry, ruff's lint and format checks, ShellCheck over the shell scripts),
  `types` (pyright),
  `unit` (the pytest roster, run in parallel),
  `benchmark` and `cdk` (one per project outside the workspace, `benchmark/` and `oops/cdk/`: each syncs the project's own `.venv` and runs pyright and pytest inside it, since the workspace venv cannot import the project at all),
  the opt-in `llm` and `conformance` (live-LLM probes, each run only when `--only` names the stage, since they spend real tokens),
  and the host-only `docker` (the container entrypoint's postconditions and the launch path from a cold image tag),
  `broker_e2e` (the live broker-supervised container launch seam, `ride/ride/e2e_test.py`),
  and `webview_e2e` (the real browser worker route, `browser/bro/webview/e2e_test.py`),
  all skipped when the gate itself runs inside a container.
  `--only` and `--skip` name stages, are repeatable, and are mutually exclusive.
  `--shard K/N` runs the K-th of N shards of the `broker_e2e` stage (`bro.dev.sharding` deals them), for a runner per shard.
  Every selected stage runs whatever the ones before it did, so one pass reports every problem the tree has.
  Before any stage, the gate refuses to start while a roster entry names no file or a `*_test.py` of the checkout sits in no roster or in several;
  a module run only by hand sits in a roster no stage runs.
  The gate keeps its commands' output to itself:
  it prints a line as a stage starts and its verdict with the elapsed time as it ends,
  and closes on the whole output of every failed command under a header naming its stage and step, then the one-line verdict per stage;
  `--verbose` streams the commands' output as they run, and on a terminal the stages draw as a live table with pytest's progress.
  Color follows `--color` (by default, whether stderr is a terminal) and is passed down to the commands, which see only a pipe.
  `--changed` narrows the gate to what a diff against `--base` (default `origin/master`) can reach through the repository's import graph (`bro.dev.affected_tests`):
  `unit` drops the test modules the change cannot reach
  — a roster module with no source module of its own holds a repository-wide invariant and runs whatever changed;
  `lint` runs `sync-scripts` and deptry only for the distributions the change lands in, leaving both ruff checks and ShellCheck repo-wide;
  a project's stage is skipped whole unless the change reaches something that project imports or edits any project's metadata;
  and `types` is never narrowed, since pyright loads the dependency closure whatever file list it is given, so a shorter list hides errors instead of skipping work.
  Each stage names the scope it ran, and a stage the narrowing drops reads `skipped` in the closing verdict rather than going missing from it.
  A stage that passes over a clean worktree is noted on the tree `HEAD` names,
  and a later run on that tree skips it where the note covers its work, the whole stage or the same narrowed share, and reads `ok` for it;
  `--rerun` runs it anyway.
  `run-tests --changed --base origin/<base>`, `<base>` the pull request's base, is the pre-push gate;
  the whole gate is the pull request's, a runner per stage and per `broker_e2e` shard, with `webview_e2e` on its own runner (`.github/workflows/tests.yml`, on `pull_request`, on `push` to `master`, and on `workflow_dispatch`
  — a push to a feature branch triggers nothing, so a branch that never opens a PR runs on a dispatch or not at all)
- `run-tests --only llm` — the live-LLM behavior probes (`*_llm_test.py`):
  a real bro against the configured provider, asserting on the artifacts it produces.
  They spend real tokens, so the stage lists them and runs only when named, and pytest collects one only as a file named on its command line, never by walking a directory (`conftest.py`);
  the benchmark project holds its graded trials out of directory collection the same way
- `run-tests --only conformance` — the conformance probes (`*_conformance_test.py`):
  each holds one behavior of the framework's contract on every installed harness, under every recipe `bro/local/conformance_test_helper.py` lists as supported there, which is what a pass certifies.
  A probe launches an unattended unboxed `ride solo` as an operator would and reads its verdict off the trail the session records locally;
  an installed harness with no listed recipe fails collection.
  The same collection rule as `llm` keeps them out of directory walks

The root `conftest.py` owns test isolation:
it rebuilds the suite's environment through `bro.base.suite_environment.rebuild_environment` (`bro/base/AGENTS.md`), as does each conftest at the root of a project outside the workspace,
so a run launched from inside a managed session inherits none of it and resolves only what a test installed itself;
`local/bro/local/environment_policy_test.py` holds every pytest root and each part of the sweep to it repository-wide.
