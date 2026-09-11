# Benchmark launcher

Launcher-side integration for benchmark runs.
The workspace member publishes `bro-bench`, and its package is `bro.bench`, a portion of the framework's `bro` namespace.
It carries only code the root environment must install so launches can register benchmark credentials, broker work, and session commands without importing Harbor.

## Development

This member depends on core and joins the root workspace environment.
Formatting, linting, typing, tests, and packaging use the root repository gate.
Run `sync-scripts --project bench` after adding or removing a CLI, and build the wheel with `uv build --package bro-bench`.

## Components

- `bro/bench/credentials.py` — the `harbor` and `benchmark_retention` credential-kind declarations.
- `bro/bench/job.py` (`benchmark-job`) — the `benchmark` broker-kind handler and the session client that starts or checks a raw Harbor run.
- `bro/bench/run.py` (`benchmark-run`) — the operator loop that narrows a config, builds the trial bundle, starts the broker job, and reports its raw artifact.
