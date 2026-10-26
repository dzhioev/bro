# Messages wire

`bro-wire-messages` contributes the SDK-free `bro.wire_messages.decode` module through `bro.llm.wires`.
The package hub imports nothing.
`decode.py` owns response parts, stops and billing counts; it imports no SDK or harness.
The root repository owns formatting and the test gate.
Build with `uv build --package bro-wire-messages`.
