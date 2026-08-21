# bro-native

`bro-native` runs a declared bro through the framework's native LLM loop.
It installs the `bro`
command, including the `run`, `chat`, `list`, and `show` surfaces, plus the native provider diagnostic
CLI.

Install `bro-native` beside the core `bro` distribution
in any environment that launches the native
harness.
`bro-ride` deliberately does not depend on it because a Claude-only runtime does not need an
in-process LLM engine.
