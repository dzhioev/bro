# Broker messaging substrate

`bro.broker` is the consumer-neutral host↔peer messaging substrate.
It owns the wire, transport, journal, Worker supervision, and dispatch logic;
it imports neither `ride` nor the bro class graph.

## Protocol

`brotocol.py` owns four closed envelope types:

- `request {id, payload: {kind, args}}` opens a quest.
- `mark {quest, payload}` carries `accepted`, `started`, or `trail`.
- `progress {quest, payload}` carries kind-defined interim data.
- `result {quest, payload}` closes the quest exactly once.

Mark origin is structural:
`accepted` is dispatcher-born, `started` is Worker-born, and `trail` is the only mark a worker process may send.
`MAX_FRAME_BYTES` is the encoded-frame bound;
the TCP adapter owns NDJSON framing and the attach handshake.

## Layers

- `transport.py` defines the async host and synchronous client ports, channel provisioning, and URI dispatch.
- `transports/tcp.py` serves every host-minted channel on one listener.
  The secret attach token authenticates and attributes a channel;
  a new accepted attach supersedes its predecessor.
- `spawn.py` defines the `Spawner` / `ChildHandle` launch port and the bounded output-tail buffer.
- `job.py` launches mute host jobs in their own process group and owns the run-directory layout.
- `runtime.py` is shape-free mechanism:
  transport serving, per-channel connect/disconnect demultiplexing, send, provision/close, and process launch helpers.
- `worker.py` owns supervision by shape.
  `SpawnedWorker` drains the channel before deciding from process reap;
  `JobWorker` collects the run directory through `JobOutput` and answers from reap;
  `ExpectedWorker` treats attach as start and EOF as death because no host child handle exists.
  The shared Worker base owns wait-task teardown and the two-phase deadline:
  the fixed launch bound is replaced by the request timeout at `started`.
- `journal.py` owns one mutable record per worker-backed quest, the ordered event ring, and permanent lineage.
  Every projection subscribes to its one append funnel;
  a raising subscriber is logged without breaking later subscribers.
- `dispatcher.py` routes over journal records, binds one Worker per worker-backed quest, synthesizes failure from Worker death, and serves the reserved `query` / `events` read kinds.
  Its handler vocabulary is `reply`, `deny`, `spawn`, `job`, and `expect`.
- `client.py` is the synchronous peer handle for requests, marks, progress, results, and correlated waits.
- `broxy.py` is the stateless session multiplexer:
  it holds one upstream channel, authenticates local clients with one shared token, and keeps sticky quest-to-connection routes only until local EOF or result delivery.
  Local delivery never drains;
  a reply whose waiter died is dropped because recovery reads the host journal.
  `MAX_ROUTES` is a leak backstop, not retention.
- `cli.py` exposes the low-level broker request and receive surface.

## Journal

A `Record` stores quest id, kind, parent quest, requester, worker, bounded args, folded lifecycle, trail id, and the retained result.
Worker-backed authorization calls `Journal.open`, which appends `accepted`;
`Dispatcher.deny` creates a terminal denial record.
Inline and read kinds answer without records.

The event sequence is monotone for the broker root.
Events carry their own quest, kind, parent, transition, timestamp, and transition payload.
The retention ladder exempts live records:
retained result payloads age out first, then terminal records, while lineage remains for the session lifetime.
The event ring is independently bounded.
The bounds live with the journal constants.

Args share one bounded-head implementation for memory and audit.

`query` returns caller-scoped, frame-bounded live-first pages with an opaque continuation cursor;
it also supports a terminal wait by id.
`events` returns caller-scoped ordered batches after a cursor and supports bounded long-polling.
Both clamp waits to 600 seconds, are answered inline, and never record themselves.
A caller sees the quests it requested and their descendants according to permanent journal ancestry;
it never sees the parent-owned quest that its own worker answers.

## Dispatcher invariants

The three routing rules are:

1. a live quest accepts marks, progress, and a result only from its bound worker;
2. a request invokes its one registered kind handler, unless its id already exists in lineage;
3. every other message is dropped and logged.

A process-sent mark is accepted only for a first, non-empty `trail`.
A Worker-generated `started` mark folds into the journal before forwarding.
A delivered or synthesized result folds `ended` and removes the record from the live index;
the worker index remains until Worker death so a live session can keep requesting work after answering its parent quest.

The host root is a normal `SpawnedWorker` on a host-anchored journal record.
Root exit closes every live record as `killed`, or `detached` for expected workers, before Worker teardown.

`deny` is for refused worker-backed work:
it sends `result{denied}` and journals the denial in one call.
Read and inline handlers return their own denied result through `reply`, avoiding recursive read records.
Unknown kinds and lineage collisions are dispatcher wire denials and remain unjournaled.

## Tests

- `brotocol_test.py` covers envelope validation, builders, accessors, and the frame cap.
- `transports/tcp_test.py` covers attach authenticity, supersession, framing, delivery, disconnect, and shutdown over real sockets.
- `runtime_test.py` covers the shape-free transport and launch seam.
- `worker_test.py` covers each supervision shape, start timing, timeout kill, collection, and death reports.
- `journal_test.py` covers folding, subscribers, retention, lineage, bounds, event gaps, and ancestry scope.
- `dispatcher_test.py` covers routing, origin checks, journaled denial, Worker synthesis, and the read kinds.
- `job_test.py` and `spawn_test.py` cover the process and launch ports.
- `client_test.py`, `cli_test.py`, and `broxy_test.py` cover the peer-facing and stateless proxy surfaces.
