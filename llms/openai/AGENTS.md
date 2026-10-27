# Openai provider

`bro-openai` contributes the `openai` provider through `bro.llm.providers` and its credential kind through `bro.credentials`.
`bro.openai.declaration` owns recipes, models, pricing and failure signatures.
The package hub imports nothing; `credentials.py` is an import-free literal dictionary.
The declaration imports no SDK at module level and no harness.
Its wire is provided by `bro-wire-responses[client]`.

Formatting, typing and tests use the root gate.
Build with `uv build --package bro-openai`.
