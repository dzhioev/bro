# bro-native

`bro-native` runs a declared bro through the framework's native LLM loop.
It installs the `bro` command, including the `run`, `chat`, `list`, and `show` surfaces, plus the native provider diagnostic CLI.
It also registers the `bro` session harness through `bro.harnesses`.

Install `bro-native` beside `bro-ride` in any environment that launches the native harness.
`bro-ride` alone is a Claude-only runtime;
it discovers only the Claude harness and never imports the native engine.
