# Chat wire

`bro-wire-chat` contributes the SDK-free `bro.wire_chat.decode` module through `bro.llm.wires`.
The package hub imports nothing.
`decode.py` owns response parts, stops and billing counts; it imports no SDK or harness.
`client.py:Chat` implements the core `Wire` port with client-side history, assistant reasoning pass-back and step-based replay.
It registers in `bro.llm.wire_clients`; install the `client` extra for its SDK.
`structured` uses JSON mode, a prompt-carried schema and pydantic validation without conversation state.
The root repository owns formatting and the test gate.
Build with `uv build --package bro-wire-chat`.
