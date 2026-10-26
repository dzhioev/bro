# Native trail format

`bro-native-trails` registers the `bro` trail format from `bro/native_trails/format.py`.
It owns the native envelope, classification, header fields and projection, decoding each LLM payload through the wire roster in core.
It depends on core alone;
neither a harness nor a provider SDK is needed to read native trails.

Formatting, linting, typing and the test gate belong to the repository root.
Build with `uv build --package bro-native-trails`.
The package `__init__.py` stays empty.
