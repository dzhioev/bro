# Broker messaging substrate

`bro.broker` is the consumer-neutral host↔peer messaging substrate.
It owns the wire, transport, journal, Worker supervision, and dispatch logic;
it imports neither `ride` nor the bro class graph.

## Protocol

`brotocol.py` owns four closed envelope types:

- `request {id, payload: {kind, args}}` opens a quest.
- `mark {quest, payload}` carries `accepted`, `started`, `trail`, or `listening`.
- `message {quest, payload, id?, reply_to?}` carries kind-defined chat traffic in either direction.
- `result {quest, payload}` closes the quest exactly once.
  Its optional error and detail reason are strings.

A message with neither role field is a say, one with `id` is a question, and one with `reply_to` is a reply;
a reply may carry both fields to ask a counter-question.
Every quest fixes a subset of `requester.say`, `requester.question`, `worker.say`, and `worker.question` as its talk when it opens.
A reply needs the other end's question right, and a counter-question also needs the sender's question right.
Mark origin is structural:
`accepted` is dispatcher-born, `started` is Worker-born, and a worker process may send `trail` or `listening`.
`MAX_FRAME_BYTES` is the encoded-frame bound;
`MAX_IDENTIFIER_BYTES` keeps quest, kind, trail, and chat identifiers small enough for journal projections.
The TCP adapter owns NDJSON framing and the attach handshake.
An accepted attach answers `ok <PROTOCOL_REVISION>`, and both client adapters refuse a missing or differing revision before messages flow.

## Layers

- `transport.py` defines the async host and synchronous client ports, channel provisioning, and URI dispatch.
- `transports/tcp.py` serves every host-minted channel on one listener.
  The secret attach token authenticates and attributes a channel;
  a new accepted attach supersedes its predecessor.
- `spawn.py` defines the `Spawner` / `ChildHandle` launch port and the bounded output-tail buffer.
  Every spawner receives the quest's talk and publishes it as sorted, comma-joined `BROKER_TALK` beside `BROKER_QUEST`.
- `job.py` launches mute host jobs in their own process group and owns the run-directory layout.
- `runtime.py` is shape-free mechanism:
  transport serving, per-channel connect/disconnect demultiplexing, send, provision/close, and process launch helpers.
- `worker.py` owns supervision by shape.
  `SpawnedWorker` drains the channel before deciding from process reap;
  `JobWorker` collects the run directory through `JobOutput` and answers from reap;
  `ExpectedWorker` treats attach as start and EOF as death because no host child handle exists;
  its pre-acceptance preparation runs off-loop before the ready mark.
  The shared Worker base owns wait-task teardown and the two-phase deadline:
  the fixed launch bound is replaced by the request timeout at `started`.
  `Worker.end(reason)` is the host-initiated end:
  it kills the worker's process, or for an expected Worker ends its channel, and the death the worker then reports carries `reason`;
  the deadline is `end('timeout')`.
  An end accepted before the worker started reports its death only once the launch or off-loop preparation it interrupted has settled, whether that returned a handle to reap or failed, and the accepted reason is what the death carries.
- `journal.py` owns one mutable record per worker-backed quest, the ordered event ring, and permanent lineage.
  Every projection subscribes to its one append funnel;
  a raising subscriber is logged without breaking later subscribers.
- `dispatcher.py` routes over journal records, binds one Worker per worker-backed quest, synthesizes failure from Worker death, and serves the reserved `query` / `events` read kinds and the `cancel` kind.
  Its handler vocabulary is `reply`, `deny`, `spawn`, `job`, and `expect`.
  Delivery fits an oversized generated result into a correlated failure or denial with a truncation marker.
- `client.py` is the synchronous peer handle for requests, lifecycle answers, chat messages, listeners, and both request- and reply-correlated waits.
  It reads the launched peer's own quest and talk from `BROKER_QUEST` and `BROKER_TALK`, refusing a disallowed own-quest move before sending.
  `BROKER_CHANNEL` is its client address;
  `BROKER_UPSTREAM` without a channel means the session proxy failed at launch and raises with the broxy log path.
- `broxy.py` is the stateless session multiplexer:
  it holds one upstream channel, authenticates local clients with one shared token, and routes inbound traffic first by reply id, then by a request's quest id, then to every listener for the quest.
  Routes live until result delivery for a request or local EOF for questions and listeners.
  Local delivery never drains;
  a reply whose waiter died is dropped because recovery reads the host journal.
  `MAX_ROUTES` bounds request, question, and listener registrations together as a leak backstop, not retention.
- `cli.py` exposes the low-level broker request, chat message, receive, and listen surface.

## Journal

A `Record` stores quest id, kind, parent quest, requester, worker, bounded args, folded lifecycle, trail id, retained result, and the quest's talk.
It also stores `listening`, the bounded pending-question set, the bounded message/refusal tail with each entry's transition, and `chat_seq`, the journal sequence of its latest message or refusal.
Worker-backed authorization calls `Journal.open`, which appends `accepted`;
`Dispatcher.deny` creates a terminal denial record.
Inline and read kinds answer without records.

The event sequence is monotone for the broker root.
Events carry their own quest, kind, parent, args, transition, timestamp, and transition payload.
`message`, `refused`, and `listening` are the chat transitions.
A journaled message carries its payload whole, at most `MAX_MESSAGE_BYTES`;
the dispatcher refuses a larger one, and only a refused entry's head is bounded like args, since its size can be what was refused.
The retention ladder exempts live records:
retained result payloads age out first, then terminal records, while lineage remains for the session lifetime.
The event ring is independently bounded.
The bounds live with the journal constants.

Args share one bounded-head implementation for memory and audit:
a dict over budget keeps its top-level scalar fields, dropping the largest while they overflow the budget on their own, and collapses the rest into a JSON head marked `truncated`.
Trail ids and terminal reasons use a bounded journal projection with an explicit truncation marker.

`query` returns caller-scoped, frame-bounded live-first pages with an opaque continuation cursor;
it supports a terminal wait by id, `since` returns that wait when `chat_seq` advances, and `result_evicted` reports a retained result that cannot fit its response frame.
The listing view carries talk, listening, pending questions, and chat sequence, while the by-id view also carries the chat tail.
A by-id response keeps every pending question and the newest suffix of the tail that fits the frame;
`messages_truncated` marks omitted older tail entries.
The retained result has priority over the tail and becomes `result_evicted` only when it cannot fit after the tail is removed.
`events` returns caller-scoped, frame-bounded ordered batches after a cursor and supports bounded long-polling.
Both clamp waits to 600 seconds, are answered inline, and never record themselves.
A caller sees the quests it requested and their descendants according to permanent journal ancestry.
It may also query the quest its own worker answers by id and see that quest's three chat transitions, but not list it or see its lifecycle transitions.

## Dispatcher invariants

The three routing rules are:

1. a live quest accepts marks and a result only from its bound worker;
2. a `message` is routed by the quest it names when it comes from either bound end, its payload fits `MAX_MESSAGE_BYTES`, and its role is in the quest's talk, then delivered to the other end and journaled;
3. a request invokes its one registered kind handler unless its id already exists in lineage, and every other envelope is dropped and logged.

A chat envelope from a stranger is dropped, while one from a bound end over `MAX_MESSAGE_BYTES` or without the required talk right is also journaled as `refused` with its correlation fields.
Delivery to an absent receiver is dropped and logged rather than buffered;
the journal remains the inbox of record.
A process-sent `trail` is accepted only once with a non-empty id.
A process-sent `listening` is also set once, with repeats accepted silently.
A Worker-generated `started` mark folds into the journal before forwarding.
A delivered or synthesized result folds `ended` and removes the record from the live index;
the worker index remains until Worker death so a live session can keep requesting work after answering its parent quest.
A record's marks and result are delivered only while its requester is still in that index.

A Worker's death orphans every live quest its peer requested:
each of those Workers is ended with reason `orphaned`, and the death it then reports ends its record `failed{orphaned}` and orphans the quests it requested in turn, so the cascade follows the tree.
`cancel {id}` ends one live quest for the peer that requested it:
the Worker is ended with reason `cancelled` and the request is answered `ok` inline, while the quest's `failed{cancelled}` result follows the reap;
any other id is denied.
While a Worker's end is accepted, a result its process sends is refused, so the reap owns the quest's single terminal.
Like the read kinds, `cancel` is answered inline and never recorded.

The host root is a normal `SpawnedWorker` on a host-anchored journal record.
Root exit is not a cascade:
it closes every live record as `killed`, or `detached` for expected workers, before Worker teardown.
A caller that owns its process opts in with `run(root, end_on_sigterm=True)`:
the run then holds SIGTERM until its teardown is over, the signal ending it the same way rather than killing the process, with the root exit reporting `TERMINATED_EXIT_CODE` before the teardown reaches every worker the run started;
the default disposition is what it leaves behind.
A run that does not opt in touches no process signal, so an embedder may drive it from any thread.

`deny` is for refused worker-backed work:
it sends `result{denied}` and journals the denial in one call.
Read and inline handlers return their own denied result through `reply`, avoiding recursive read records.
Unknown kinds and lineage collisions are dispatcher wire denials and remain unjournaled.

## Tests

- `brotocol_test.py` covers envelope validation, builders, chat roles, talk encoding and enforcement, and the frame cap.
- `transports/tcp_test.py` covers attach authenticity and revision checks, supersession, framing, delivery, disconnect, and shutdown over real sockets.
- `runtime_test.py` covers the shape-free transport and launch seam.
- `worker_test.py` covers each supervision shape, start timing, the host-initiated end and its timeout case, collection, and death reports.
- `journal_test.py` covers folding, subscribers, retention, lineage, bounds, event gaps, and ancestry scope.
- `dispatcher_test.py` covers routing, origin checks, journaled denial, Worker synthesis, the requester-death cascade, the cancel kind, and the read kinds.
- `job_test.py` and `spawn_test.py` cover the process and launch ports.
- `client_test.py`, `cli_test.py`, and `broxy_test.py` cover the peer-facing and stateless proxy surfaces.
