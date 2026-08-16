# benchmark/CLAUDE.md

Driving a bro as an agent under Harbor, the harness Terminal-Bench runs on. The distribution is
`bro-benchmark` and its package is `bro.benchmark`, a portion of the framework's `bro` namespace;
nothing in the framework imports it. What the project is and how to drive it is `README.md`.

## Development

This is not a uv workspace member: `harbor` reaches `openai < 3` through litellm and the framework's
`agent` extra pins `openai == 3`, so no single lock satisfies both. The directory therefore locks and
syncs on its own — `uv sync --directory benchmark --all-groups` builds `.venv` from the `uv.lock`
committed beside this file — and depends on the framework through
`bro = { path = "..", editable = true }` with no extras. Keep it that way: the host side of this
project reads only the framework's base layer, never the class graph that pulls in the other
`openai` major, and the two must never meet in one interpreter.

Formatting and linting stay the repository root's, which walks this directory through its
`[tool.ruff] src`. pytest and pyright run from here instead, inside `.venv`: `run-tests` syncs it and
drives both, and `--no-benchmark` skips that whole stage. Build the wheel with `uv build` from this
directory rather than `uv build --package`.

`bundle_e2e_test.py` stays out of the gate's roster: it builds a bundle and drives the host docker
daemon, the way `bro/launch/e2e_test.py` does. Run it explicitly, from `.venv`:

```
uv run --directory benchmark pytest bro/benchmark/bundle_e2e_test.py
```

## Components

- `bro/benchmark/bundle.py` (`benchmark-bundle`) — builds the relocatable directory a foreign
  container runs `bro` from: a pinned standalone CPython, `bro[agent]` resolved from the workspace
  lock with the framework entering as a built wheel so `bros/*` ships with it, and a shim setting
  `PYTHONPATH` over the two. `Bundle` is the layout a consumer addresses — shim, interpreter,
  site-packages, and the CA store to point `SSL_CERT_FILE` at; `built(root)` reports an absent or
  incomplete bundle rather than building one behind the caller's back
