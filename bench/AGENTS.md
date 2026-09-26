# Benchmark launcher

Launcher-side integration for benchmark runs.
The workspace member publishes `bro-bench`, and its package is `bro.bench`, a portion of the framework's `bro` namespace.
It carries only code the root environment must install so launches can register benchmark credentials, the benchmark worker type, and session commands without importing Harbor.

## Development

This member depends on core and joins the root workspace environment.
Formatting, linting, typing, tests, and packaging use the root repository gate.
Run `sync-scripts --project bench` after adding or removing a CLI, and build the wheel with `uv build --package bro-bench`.

## Components

- `bro/bench/credentials.py` — the `harbor` and `benchmark_retention` credential-kind declarations.
- `bro/bench/job.py` (`benchmark-job`) — the registered `BenchmarkType`, whose launch schema has no fields, and the session client that starts or checks a raw Harbor run through `launch`.
  A caller needs `:launch.benchmark` (`bro/reference/ride.md`, "Session permissions and credentials").
- `bro/bench/presets.py` — the presets under `benchmark/` (agents, settings, datasets, tasks): their shapes, and their composition into a Harbor job config carrying the names it came from.
- `bro/bench/run.py` (`benchmark-run`) — the operator loop around one run:
  it composes the presets, builds the trial bundle, runs the job through the session broker or, outside a session, as a host process into a jobs directory, reports it, and retains it on request.
