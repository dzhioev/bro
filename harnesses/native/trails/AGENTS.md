# Native trail format

`bro-native-trails` registers the `bro` trail format from `bro/native_trails/format.py`.
It owns the native envelope, classification, header fields and projection, decoding each LLM payload through the wire roster in core.
It reads line rows holding one run log step line verbatim in `body` under `run_log`, alongside spread rows holding the step fields directly.
Its `native_record` exposes either shape as a step for classification, projection and continuation readers;
stored-row reads and transfers preserve the line.
`STEP_LINE_FORMAT` versions that envelope independently of the native run log's file format.
It depends on core alone;
neither a harness nor a provider SDK is needed to read native trails.

Formatting, linting, typing and the test gate belong to the repository root.
Build with `uv build --package bro-native-trails`.
The package `__init__.py` stays empty.
