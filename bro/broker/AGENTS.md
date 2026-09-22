# Broker messaging substrate

`bro.broker` is the consumer-neutral host↔peer messaging substrate.
It owns the wire, environment names, transport, journal, host supervision, and dispatch logic.
Every module in the package imports only the standard library, `bro.base`, and `bro.broker`;
test modules may also import `pytest`.
The repository enforces that boundary in `local/bro/local/import_policy_test.py`.

## Vocabulary and protocol

A mission is one unit of work an owner gives and one worker undertakes.
The request opening a mission and the mission itself share an id.
A mission's parent is the mission its owner undertakes.

`brotocol.py` owns four closed envelope types:

- `request {id, payload: {kind, args}}` asks the host to do work.
- `mark {request, payload}` carries `accepted`, `started`, `trail`, or `listening` for that request.
- `message {request, payload, id?, reply_to?}` carries chat traffic between a mission's ends.
- `result {request, payload}` answers a request exactly once.
  Its optional error and detail reason are strings.

`Message.request_id` is the request id for every envelope shape.
A chat message with neither role field is a say, one with `id` is a question, and one with `reply_to` is a reply;
a reply may carry both fields to ask a counter-question.
Every mission fixes a subset of `owner.say`, `owner.question`, `worker.say`, and `worker.question` as its talk when it opens.
A reply needs the other end's question right, and a counter-question also needs the sender's question right.
Mark origin is structural:
`accepted` is dispatcher-born, `started` is supervisor-born, and a worker process may send `trail` or `listening`.

`MAX_FRAME_BYTES` is the encoded-frame bound.
`MAX_IDENTIFIER_BYTES` bounds the encoded cost of request, kind, trail, and chat identifiers inside a frame.
The TCP adapter owns NDJSON framing and the attach handshake.
An accepted attach answers `ok <PROTOCOL_REVISION>`, and both client adapters refuse a missing or differing revision before messages flow.

## Environment

`environment.py` owns the broker's four environment names:
`BROKER_CHANNEL`, `BROKER_UPSTREAM`, `BROKER_MISSION`, and `BROKER_TALK`.
The module contains constants only, so launch paths may import it before the broker-enabled gate without loading broker machinery.
`BROKER_MISSION` names the mission the process undertakes.
`BROKER_TALK` is the mission's sorted, comma-separated talk rights.
`BROKER_CHANNEL` is the address clients connect to.
`BROKER_UPSTREAM` is the host address a peer-side broxy consumes.

## Layers

- `transport.py` defines the async host and synchronous client ports, channel provisioning, and URI dispatch.
- `transports/tcp.py` serves every host-minted channel on one listener.
  The secret attach token authenticates and attributes a channel;
  a new accepted attach supersedes its predecessor.
- `spawn.py` defines the `Spawner` / `ChildHandle` launch port and the bounded output-tail buffer.
  Every spawner publishes `BROKER_MISSION` and `BROKER_TALK` beside the channel.
- `job.py` launches mute host jobs in their own process group and owns the run-directory layout.
- `runtime.py` owns shape-free transport serving, channel demultiplexing, send, and provision/close.
- `supervisor.py` owns supervision by shape.
  `SpawnedSupervisor` drains the channel before deciding from process reap;
  `JobSupervisor` collects the run directory through `JobOutput` and answers from reap;
  `ExpectedSupervisor` treats attach as start and EOF as death because no host child handle exists.
  The shared `Supervisor` base owns wait-task teardown and the two-phase deadline.
- `journal.py` owns one mutable record per worker-backed mission, the ordered event ring, and permanent lineage.
  Every projection subscribes to its one append funnel;
  a raising subscriber is logged without breaking later subscribers.
- `dispatcher.py` routes over journal records, binds one supervisor per worker-backed mission, synthesizes failure from supervisor death, and serves `query`, `events`, and `cancel`.
  Its handler vocabulary is `reply`, `deny`, `spawn`, `job`, and `expect`.
  `spawn`, `job`, and `expect` require the worker type they record;
  `spawn` also takes the spawner for its launch, while `spawn` and `job` require the caller-owned timeout (`None` is unbounded);
  `deny` accepts an optional type for requests that named one.
- `client.py` is the synchronous peer handle for requests, lifecycle answers, chat messages, listeners, and request- and reply-correlated waits.
  It reads the launched peer's mission and talk from `BROKER_MISSION` and `BROKER_TALK`, refusing a disallowed worker move before sending.
  `BROKER_UPSTREAM` without a channel reports that the session proxy failed at launch.
- `broxy.py` is the stateless peer multiplexer.
  It routes inbound traffic first by reply id, then by request id, then to every listener for the request.
  `broxy run [--log-file PATH] -- <command…>` attaches once through `BROKER_UPSTREAM`, gives the command the local `BROKER_CHANNEL`, forwards SIGTERM, and returns the command status.
  Worker-container commands use this wrapper so the worker and its short-lived artifact or broker clients share one upstream attach.
- `cli.py` exposes the low-level broker request, chat message, receive, and listen surface.

## Journal

A `Record` stores mission id, kind, worker type, parent mission, owner, worker, bounded args, folded lifecycle, trail id, retained result, supervision settlement, and talk.
An accepted record requires its worker type.
A denial may have no usable type;
its record and events keep `type` unset and omit it from views.
An `Event` carries `mission`, kind, type when known, parent, bounded args, transition, timestamp, and transition payload.
The list query answers `{missions: […], cursor?}` and a by-id query answers `{mission: view}`.
A worker result ends the record, while `settled: true` appears after its supervisor reaps the execution and finishes host cleanup;
a by-id query with `settled: true` and `wait` can long-poll for that second boundary.
An evicted view retains only id, kind, parent, and state because lineage is unchanged.

The event sequence is monotone for the broker root.
`message`, `refused`, and `listening` are chat transitions.
A journaled chat entry carries `from: owner|worker`.
A journaled message carries its payload whole up to `MAX_MESSAGE_BYTES`.
The retention ladder exempts live records:
retained result payloads age out first, then terminal records, while lineage remains for the session lifetime.
The event ring is independently bounded.

Args share one bounded-head implementation for memory and audit.
Trail ids and terminal reasons use a bounded journal projection measured by the same encoded cost.
A caller sees the missions it owns and nothing beneath them.
It may query the mission its worker undertakes by id and see that mission's chat transitions, but it cannot list that mission or see its lifecycle transitions.

## Dispatcher invariants

The three routing rules are:

1. A live mission accepts marks and a result only from its bound worker.
2. A chat message is routed by its request id when it comes from either bound end, fits `MAX_MESSAGE_BYTES`, and its role is in the mission's talk.
3. A request invokes its registered kind handler unless its id already exists in lineage;
   every other envelope is dropped and logged.

Delivery to an absent receiver is dropped and logged rather than buffered;
the journal remains the inbox of record.
A process-sent `trail` is accepted only once with a non-empty id.
A process-sent `listening` is also set once.
A supervisor's worker death orphans every live mission its peer owns, cascading through the mission tree.
`cancel {id}` ends one live mission for its owner.
Root exit closes every live record as `killed`, or `detached` for expected workers, before supervisor teardown.

`deny` is for refused worker-backed work:
it sends `result{denied}` and journals the denial in one call.
Read and inline handlers return their own denied result through `reply`.
Unknown kinds and lineage collisions are wire denials and remain unjournaled.

## Tests

- `brotocol_test.py` covers envelope validation, builders, chat roles, talk encoding, revision, and the frame cap.
- `transports/tcp_test.py` covers attach authenticity and revision checks, supersession, framing, delivery, disconnect, and shutdown.
- `runtime_test.py` covers the shape-free transport runtime.
- `supervisor_test.py` covers each supervision shape, start timing, host-initiated end, timeout, collection, and death reports.
- `journal_test.py` covers folding, worker types, subscribers, retention, lineage, bounds, event gaps, and caller scope.
- `dispatcher_test.py` covers routing, type threading, origin checks, denial, supervisor synthesis, orphaning, cancel, and reads.
- `job_test.py` and `spawn_test.py` cover the process and launch ports.
- `client_test.py`, `cli_test.py`, and `broxy_test.py` cover the peer-facing and proxy surfaces.
