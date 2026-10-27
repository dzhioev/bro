# Responses wire

`bro-wire-responses` contributes the SDK-free `bro.wire_responses.decode` module through `bro.llm.wires`.
The package hub imports nothing.
`decode.py` owns response parts, stops and billing counts; it imports no SDK or harness.
`client.py:Responses` implements the core `Wire` port, server-side chaining, replay sanitization and reply extraction.
It registers in `bro.llm.wire_clients`; install the `client` extra for its SDK.
The recipe supplies request options and retry classification.
The root repository owns formatting and the test gate.
Build with `uv build --package bro-wire-responses`.
