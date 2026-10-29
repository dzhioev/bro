# DeepSeek provider

`bro-deepseek` registers its provider declaration and credential kind through `bro.llm.providers` and `bro.credentials`.
`bro.deepseek.declaration` owns model recipes, retry classification, failure signatures and peak list pricing;
computed costs are upper bounds because off-peak calls cost less.
The package hub imports nothing and the credential module is an import-free literal dictionary.
The declaration imports no SDK at module level and no harness.
The bare distribution depends on `bro-wire-chat`; its `client` extra requests that wire's `client` extra.

Formatting, typing and tests use the root gate.
Build with `uv build --package bro-deepseek`.
`deepseek_llm_test.py` is an opt-in live probe that saves raw replies in pytest's temporary directory.
