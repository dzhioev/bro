# bro-benchmark

`bro-benchmark` runs a bro as an agent under [Harbor](https://github.com/harbor-framework/harbor),
the harness that distributes and executes Terminal-Bench. It ships as `bro.benchmark`, a portion of
the framework's `bro` namespace package, from a project of its own beside the framework workspace.

Sync its environment before using it — the repository's `./setup.sh` leaves it alone:

```
uv sync --directory benchmark --all-groups
```

## The bundle

A benchmark task runs in a foreign image that must not be modified, and several carry no Python at
all, so the agent brings its own. `benchmark-bundle` builds a relocatable directory holding a pinned
standalone CPython, `bro[agent]` resolved from the framework's lock, and a `bro` shim over the two:

```
uv run --project benchmark benchmark-bundle
```

It lands in `var/benchmark/bundle` unless `--output` says otherwise, and rebuilding it from one
commit reproduces the same contents. Copying the directory somewhere is the whole installation, and
the shim inside it is the framework's `bro` command:

```
docker cp var/benchmark/bundle <container>:/installed-agent/bro
docker exec <container> /installed-agent/bro/bro show terminal
```

The bundle targets linux/x86_64 glibc, and a build refuses any other host rather than producing one
that will not run.
