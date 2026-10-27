# Anthropic provider

`bro-anthropic` contributes the `anthropic` provider through `bro.llm.providers` and its credential kind through `bro.credentials`.
`bro.anthropic.declaration` owns recipes, models, pricing and failure signatures.
The package hub imports nothing; `credentials.py` is an import-free literal dictionary.
The declaration imports no SDK at module level and no harness.
Its wire is provided by `bro-wire-messages`.

Formatting, typing and tests use the root gate.
Build with `uv build --package bro-anthropic`.
