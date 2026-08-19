# bro-native

`native/` is the `bro-native` uv workspace member. It publishes the framework's native LLM engine
and the `bro` command, and depends on the core `bro` distribution. Core and `bro-ride` never import
it. The root repository owns formatting, lint, typing, packaging policy, and the test gate. Build this
member with `uv build --package bro-native`; regenerate its scripts and committed
`bro/native/_entrypoints.py` with `sync-scripts --project native`.

## Components

- `bro/native/` — runner, live LLM contract, provider dispatch, and provider clients
- `bro/run.py` — the `bro` command dispatcher
- `bro/launch/{run,call,call_tui,resume}.py` — native one-shot and interactive launch surfaces
- `bro/fork.py` — replay and continuation of native trails
- `bro/trails/record/bro.py` — native tracker-to-trails recorder

`bro`, `bro.launch`, `bro.trails`, and `bro.trails.record` are shared namespace package trees. This
member's source root contains only native-owned leaves, so its build can publish that `bro` portion
whole without reaching another member's source tree.
