# broker messaging substrate

`broker` is the host↔peer messaging substrate for managed harnesses and bros
— the wire protocol plus the transport that carries it.
**Pure substrate:** it imports neither `ride` nor the bro class graph.
Consumers (`ride` and `bro.summon`) depend on broker through its ports;
broker depends on nothing of theirs.
This file maps what exists in the tree.

## The protocol

Three message types, closed and owned by the substrate;
every capability is a *kind* inside a request, so adding one changes no envelope, no routing, and no correlation.

- **request** — `{type, id, payload: {kind, args}}`:
  a peer opens an *exchange*, the unit of work;
  `id` names it and is minted by the sender.
- **progress** — `{type, request, payload}`:
  zero or more per exchange, informational, delivered in the order sent.
- **result** — `{type, request, payload: {outcome, value?, error?, detail?}}`:
  exactly one;
  delivering it closes the exchange.
  `outcome` is `ok` / `denied` (refused before any work began — deterministic) / `failed` (work began and produced no answer).

An exchange is one-directional after its opening request, and the guarantee everything else serves is
**every request receives exactly one result**
— a worker that dies without one gets `result{failed}` synthesized on its behalf.
There is no version field (the host provisions every channel and launches every peer, so both ends are the same release)
and no sender field:
origin is attributed by the receiving endpoint, from the channel the message arrived on.
A launched worker is handed its channel (`BROKER_CHANNEL`) and the id of the exchange it answers (`BROKER_EXCHANGE`), and correlates its own messages.

## The encoding / framing seam

The one structural rule to keep straight:

- **brotocol owns encoding** — `brotocol.py` turns a `Message` into UTF-8 JSON bytes with no delimiter (`to_bytes`) and parses them back (`from_bytes`).
  No framing.
- **the transport owns framing**
  — how messages are delimited on a byte stream.
  The unix adapter uses NDJSON (`json + '\n'`);
  a future websocket adapter would use native frames over the identical JSON encoding.

`MAX_FRAME_BYTES` (1 MiB) is the protocol constant in `brotocol.py`;
`ProtocolError` is the wire-violation exception (malformed JSON, an envelope off the three-type shape, an oversize frame), raised by both the codec and the adapter.

## Modules

- `brotocol.py` — `Message` (frozen; the codec validates the whole envelope shape at construction and parse), the `Tag` type names, the `request`/`progress`/`result` builders, the `exchange`/`kind`/`args`/`outcome` accessors, `ProtocolError`.
- `transport.py` — the ports:
  `ServerTransport` (async — its methods and the `Sink` callbacks run on the broker's event loop) / `ClientTransport` (synchronous
  — a peer is its own process) ABCs, `Sink` (the async Protocol the Runtime implements:
  `on_connect` at accept,
  `on_message` per frame,
  `on_disconnect` on a peer drop),
  `Provisioned`,
  the `Address` / `ChannelID` aliases,
  and `connect(address)` (URI scheme → client adapter).
  `ClientTransport.close(confirm=True)` blocks — deliberately unbounded
  — for the receiver's close-back, confirming everything sent was consumed:
  the guarantee a peer whose last send precedes its own exit needs (see the docstring; `BroChannel.close` opts in).
  A plain `close()` also aborts a concurrent `receive` blocked from another thread
  — the unix adapter wakes the blocked reader through an internal self-pipe before closing (neither a bare fd close nor a shutdown wakes a parked select reliably on macOS)
  — which is how a controller cancels an off-thread wait it abandoned (the bro summon tools ride on it).
- `transports/unix.py` — the v1 unix-socket adapter:
  an asyncio unix server on the host side, the synchronous socket client on the peer side.
  One bound socket file per peer under a constructor-supplied control dir (so broker stays ride-free).
- `spawn.py` — the spawn port:
  `Spawner` / `ChildHandle` (ABCs; `spawn` / `wait` / `kill` async, `output_tail` sync) + the `LaunchSpec` marker + `RingBuffer`, the bounded byte buffer behind a handle's `output_tail` (ride's spawner adapters share it).
  `spawn` receives the provisioned channel and the exchange id together
  — the worker-launch contract.
- `job.py` — the job launch:
  `CommandJob` (command, cwd, and an explicit env snapshot) run by `launch()` as a host process in its own process group — a kill takes whatever children it spawned along (SIGTERM, then SIGKILL after a grace) — with merged output ring-buffered into a `ChildHandle`.
  The process speaks no protocol; the supervision that speaks for it is `Runtime.job` + `Dispatcher.job`.
- `runtime.py` — the `Runtime`:
  the mechanism layer that owns the asyncio loop and all mutable per-peer state (channel, `ChildHandle`, the `await handle.wait()` task, the `call_later` timer, the drain event) over the two ports.
  Commands `spawn(launch, *, timeout, exchange)` / `job(command, *, timeout, exchange)` / `expect(*, timeout)` / `send` / `kill` / `forget` / `serve` / `stop`;
  emits raw, symmetric lifecycle up to a synchronous `Listener` (the `Dispatcher`):
  `on_connect` / `on_message` / `on_exit` / `on_timeout` / `on_gone`.
  No exchanges, correlation, or protocol
  — a peer is its channel;
  the root is a uniform peer.
- `dispatcher.py` — the `Dispatcher` (logic layer) + the thin `Broker` facade + the built-in kind handlers.
  The `Dispatcher` is the Runtime's `Listener`;
  it owns the live exchanges (per exchange: the requesting peer, the request id, at most one worker channel),
  the worker index (`workers`),
  the kind-handler registry (`on`),
  and the delivery observers (`add_delivery_observer`).
  `on_message` runs the three routing rules;
  `on_exit`/`on_timeout`/`on_gone`/a raising `spawn` launch synthesize `result{failed}`;
  `root` exposes the root peer's channel.
  The handler primitives `deliver` / `reply` / `refuse` / `spawn` / `job` / `expect` / `invoke` are `Dispatcher` methods a handler drives via its `context`.
  `Broker` injects the transport + spawner and exposes `on` / `add_delivery_observer` / `run(root) -> int` / `stop`;
  `ping_handler` (the reserved `ping` kind) + `spawn_test_handler` are the built-ins.
- `client.py` — the peer-side `Client` (synchronous, over `ClientTransport`):
  `from_env()` resolves `BROKER_CHANNEL` via `connect()` and returns `None` when unset;
  `send` / `request` / `call` / `await_reply` / `receive` / `close`, plus the answering side `progress` / `result` a worker emits against its exchange.
  `request` and `call` are correlate-on-receive
  — they read until a message's `request` names the sent request's id, setting uncorrelated arrivals aside for later `receive` calls;
  `TimeoutError` on deadline, `ConnectionError` on channel EOF.
  `request` returns the first correlated message;
  `call` rides through progress (surfaced to an `on_interim` callback) and returns the correlated result, the whole call under one deadline.
  `send` returns the sent request (ids are minted client-side);
  `await_reply` is `call`'s wait detached from its send
  — with an opt-in `timeout_after_interim` that re-arms the deadline on each progress, bounding silence rather than the whole wait
  — and `await_any` is `request`'s (the first correlated message as-is, what a manual summon's acceptance handshake reads), so a consumer can expose the request id before blocking;
  what `summon`'s blocking/`--detach`/`wait` modes ride on.
- `cli.py` — the `broker` console script:
  `send` / `request` / `receive` subcommands over `Client`, taking a kind and its JSON args.
  Inert (stderr note, exit 0) when `BROKER_CHANNEL` is unset;
  message output on stdout is the wire JSON, one object per line;
  `request`/`receive` exit 1 when no message arrives in `--timeout`.
- `broxy.py` — the peer-side broker proxy (`broxy` console script):
  a session-lifetime daemon holding the one upstream connection to the host broker and listening on a local unix socket that `BROKER_CHANNEL` points at,
  so the local process swarm multiplexes over the single connection upstream supersede-on-accept expects.
  Sticky per-request-id routing, a bounded in-order mailbox for waiter-gone messages, the local-only `claim` / `check` kinds
  — peer machinery above the wire protocol, deliberately not part of it.
  `launch` owns the detached `serve` spawn, log redirection, `await` gate, and failed-launch cleanup;
  invariants below.

## Unix adapter invariants

- **Single loop, no locks.**
  The server is asyncio-native:
  `provision()` starts one `asyncio.start_unix_server` per channel,
  each accepted connection runs a read task that NDJSON-deframes into the `Sink`,
  and `send()` writes through that connection's `StreamWriter`.
  Everything runs on the one event loop, so the shared per-channel state needs no lock and two coroutines can't interleave a partial frame.
  A peer that stops reading is absorbed by its own writer's `drain()` backpressure, never by stalling routing to the others.
  A host-side `close()` / `shutdown()` cancels the connection's read task so its EOF doesn't spuriously fire `on_disconnect` (that callback is reserved for peer-initiated drops).
- **Channel authenticity.**
  A connection is attributed to the `ChannelID` of the listening socket that accepted it
  — the anti-forgery primitive.
  There is no `from` field on the wire to forge.
- **Socket lifecycle.**
  `provision()` does unlink-before-bind + listen before returning (the file must exist for the bind-mount before `docker create`);
  dir `0700`, socket `0600`;
  teardown unlinks.
  The host bind path is subject to the ~108-byte `sun_path` limit, so ride uses the shallow global `<runtime-base>/broker` control dir.

## Runtime invariants

- **Death = process exit, not socket EOF.**
  A connection is transient (an fd can outlive the process, a socket can drop and reconnect while it lives), so EOF is a *channel* fact;
  the reliable death signal is `await handle.wait()`.
  A per-peer wait task emits `on_exit(peer, code, output)`.
- **Expected peers invert the death rule.**
  `expect(*, timeout)` provisions a channel for a peer someone else launches
  — no `LaunchSpec`, no `ChildHandle`.
  With no process to reap, its death signal is channel EOF after an attach (`on_gone`; sound because the attaching consumers hold one connection per run and never reconnect), and `kill` closes the channel host-side
  — the external process is not the Runtime's to kill, it just loses its channel.
  No drain step:
  every frame the peer wrote was already delivered in order before EOF.
- **Jobs invert the other half: a process with no channel.**
  `job(command, *, timeout, exchange)` provisions nothing — the peer id is synthetic (`job_peer(exchange)`, collision-free against lulid channel ids), death is process exit as for a spawned peer, the drain is skipped, and `forget` has no channel to close.
- **Drain-before-decide.**
  On process exit, before emitting `on_exit`, the Runtime waits (bounded, `_DRAIN_TIMEOUT`) for the transport to flush the channel to EOF
  — reusing `on_disconnect` as the "channel drained" marker
  — so a result the child wrote just before exiting lands as `on_message` first and the Dispatcher closes the exchange on it.
  Skipped when the peer never attached.
- **Birth = socket accepted** (`Sink.on_connect`), not the first message
  — a peer is alive from when it attaches, and a `--raw` root may never send a frame.
  The Runtime dedupes birth per peer.
- **Timeout** fires a `call_later` timer → `kill` + `on_timeout` (already killed);
  the later `on_exit` is the Dispatcher's to dedupe.
  **Launch failure** rolls back its own registration (unlinks the provisioned socket).
  **`forget`** drops the channel + timer + wait task;
  **`stop`** hard-tears-down every peer (kill + cancel) then shuts the transport down.

## Dispatcher invariants

- **The three routing rules** (`on_message`):
  (1) a progress/result naming a live exchange, arriving on that exchange's own worker channel → the requester, as-is
  — the sender must *be* the worker, so learning another exchange's id gains a peer nothing;
  (2) a request → the handler registered for its kind
  — no handler, or an id colliding with a live exchange (uniqueness rides on entropy, so a collision is rejected rather than coped with), means `result{denied}`;
  (3) anything else → refused (dropped + logged, never delivered).
- **Exactly one result per exchange.**
  A result delivery closes the exchange and a closed exchange is forgotten, so any later message naming it falls to rule 3;
  synthesis consults the same table, so whichever of the worker's own result / exit / timeout is processed first wins
  — closing the result-vs-exit and timeout-vs-result double-terminal races.
  `failed` is the only outcome the host originates on a worker peer's behalf
  — from `on_exit`-without-a-result (`reason: 'exit'`, with the exit code and output tail),
  `on_timeout` (`reason: 'timeout'`, after the Runtime already killed the peer),
  `on_gone`-without-one (`reason: 'disconnected'`, an expected peer's channel ended),
  or a `Dispatcher.spawn`/`expect` whose launch raised (`reason: 'launch'`, with the error string — no worker ever existed).
  The reason class rides `detail.reason`;
  `error` carries the free-text diagnostic.
- **A job's every message is host-originated — the third answer shape.**
  `Dispatcher.job(command, requester, *, timeout)` runs a process that speaks no protocol, so the host speaks for it:
  a started `progress{}` when the launch resolves, then a result derived from the exit
  — 0 becomes `result{ok, value: {output_tail}}`, anything else the same `failed{exit}` a mute worker produces, and the timeout / launch-failure synthesis is the worker one.
  The job exchange opens *with its worker bound* — the synthetic id is derivable up front, so no exit can slip between launch and bind.
- **Exchanges open at `spawn`/`expect`, workers bind at launch resolution.**
  `Dispatcher.spawn` opens the exchange for the in-flight request immediately (held against id collisions), threads the request id through `Runtime.spawn` to the spawner, and binds the worker channel when the launch resolves;
  `Dispatcher.expect(requester, *, timeout, ready)` does the same for an external peer and hands the provisioned channel to `ready` so the handler can publish the endpoint to whatever launches it.
  A handler that answers inline just `reply()`s — its exchange never enters the table.
- **One handler per kind.**
  `on` refuses a kind that already has a handler — two contributions claiming one name are a wiring bug, not a precedence question.
- **Delivery tap + root exposure.**
  `add_delivery_observer` registers observers fired after each correlated delivery that bypasses handlers
  — rule-1 forwarding and synthesized `failed`
  — as `(source, target, delivered message)`, with `source=None` for a launch failure and `target=None` for a host-anchored delivery;
  handler-driven `reply`, the dispatcher's own denials, and rule-3 refusals are not tapped.
  `root` exposes the root peer's channel (`None` until `run()` spawns it).
- **Uniform root on a host-anchored exchange, lock-free.**
  `run(root)` mints the session's own exchange with this process as the requester, spawns the root as its worker (no request-lifecycle timeout), and returns its exit code on `on_exit`.
  The root's started progress and closing result reach only the delivery observers;
  a result closes the exchange without gating the channel, which keeps serving the session, and a root that exits without one closes it silently
  — the host reads the exit code itself.
  The `Dispatcher` runs only inside Runtime callbacks on the one loop, so it holds no lock.

## Broxy invariants

- **One loop, no locks;
  unix-only on both sides.**
  The upstream connection and the local server share the broxy's event loop (the unix adapter's concurrency model), and both sides speak that adapter's NDJSON framing.
  Local delivery is write-only
  — no drain — so one stalled local reader can never stall routing for the others (frames per request are few and model-bounded);
  upstream forwarding does drain, inside the sending connection's own read task, which is what turns a local half-close into the `close(confirm=True)` delivery confirmation.
- **Sticky routes;
  the result ends the live exchange, not the conversation.**
  Every outbound request id maps to its sending connection;
  correlated inbound goes to exactly that connection.
  Progress keeps the route;
  the result detaches the waiter, and the conversation stays retained for cursor reads until evicted.
  Route count is capped (`MAX_ROUTES`; eviction prefers detached-empty, then collected, then any detached route).
- **Mailbox:
  retained conversations, sequence-numbered, bounded.**
  Every correlated inbound message is retained in arrival order under its request id with a 1-based sequence;
  `read_up_to` tracks the highest sequence handed to a consumer (live delivery, claim replay, or cursor read).
  Retention is what makes a result survive its own delivery
  — a waiter that died mid-collect no longer destroys the last copy.
  Over `MAILBOX_MAX_BYTES`, whole conversations drop
  — collected first (result read), then oldest detached unread, never a live in-flight wait (the bound is exceeded rather than a wait broken);
  a dropped conversation's claim/cursor read fails fast rather than replaying a gapped sequence.
  The mailbox is in-memory and session-scoped by design
  — a broxy crash loses it;
  durability is deliberately out of scope.
- **Claim = a local stand-in request;
  the wait is a lock;
  collect is one-shot.**
  The `claim` kind (args `{id}`) is never forwarded upstream.
  Unread messages are replayed
  — and future ones delivered
  — re-tagged to correlate to the claim itself, so `Client.call('claim', {'id': …})` behaves exactly like the original call did.
  Unknown id (never sent, or evicted) → immediate `result{denied}`;
  a collected conversation (result already read) → immediate `result{denied}` pointing at the cursor re-read;
  a claim while the current waiter's connection is alive → immediate `result{denied}` too (the newcomer fails fast, the live waiter keeps its route).
- **Check = the non-marking peek, or the cursor read;
  correlation separates its report from the replay.**
  The `check` kind (args `{id, last_seen?}`, local-only like claim) is always answered immediately and never supersedes a live waiter.
  Replayed window copies keep the conversation's own exchange id — no re-tag — and the check closes with its own `result{ok, value: {state, seq[, trail_id]}}`, so the reader tells the two streams apart by correlation id alone, never by payload shape.
  `state` is `pending` (no result yet), `ready` (result retained, unread), or `collected` (result read).
  Without `last_seen`:
  an unread result replays the unread window before the report, marking nothing (a later check or claim still finds it).
  With `last_seen: N`:
  replays every retained message from sequence N+1 regardless of read status
  — the recovery path for a lost delivery
  — and marks the window read;
  the window is contiguous and the report's `seq` is the new cursor;
  `last_seen` beyond `read_up_to` is denied as "from the future" (it would acknowledge messages nobody has seen).
  An unknown id and malformed args are denied.
- **Fail loudly, no restart.**
  `serve` exits 0 only on SIGTERM/SIGINT
  — the launcher's own teardown, the one expected end of a broxy
  — and 1 on anything else (an unreachable or lost upstream, a poisoned upstream frame), the socket unlinked so the session's channel disappears cleanly.
  `launch` starts it detached, redirects output to the requested log, gates on readiness, and kills it when that gate fails;
  callers decide whether a launch failure is fatal.
  The upstream is the session's own host broker over a local unix socket:
  it never comes back within a session, so a lost upstream is unrecoverable, and any other failure is a code bug to surface
  — a restart wrapper would only mask it, and the in-memory mailbox dies with the process either way.

## Tests

`brotocol_test.py` (codec round-trips per type, the builders and accessors, envelope-shape rejection at construction and parse),
`transports/unix_test.py` (a real unix-socket round-trip driving the asyncio server through a stub async `Sink`, the synchronous client run via `asyncio.to_thread`:
delivery,
`on_connect` at accept,
NDJSON framing,
oversize rejection,
two-channel authenticity,
host-close vs peer-disconnect,
a client close completing before its own concurrent reader parks in `select` still reading as EOF,
socket lifecycle),
and `spawn_test.py` (the `RingBuffer` bound),
and `job_test.py` (`CommandJob` over real processes:
merged output tail and its ring bound,
the explicit env snapshot,
exit codes,
the group-wide kill — the background child holds the output pipe, so only a group kill lets the drain reach EOF),
and `runtime_test.py` (the `Runtime` over the real asyncio transport + a non-Docker `python -c` spawner + a fake listener:
clean result with drain ordering,
the exchange id handed through the spawn port,
early-exit output tail,
timeout-kill,
send/kill/forget,
exit-before-connect,
launch-failure rollback,
the job paths
— exit + tail with no channel provisioned,
timeout-then-exit,
forget without exit,
launch-failure rollback —
and the expected-peer paths
— messages-then-`on_gone` on disconnect,
gone without a result,
kill closing the channel,
timeout-then-gone),
and `dispatcher_test.py` (the three rules + exchange closure + `failed` synthesis against a fake `Runtime`:
ping's echoed result,
unknown-kind and id-collision denials,
duplicate kind registration refused,
exchange opening with the exchange id riding the spawn,
rule-1 forwarding with the worker-channel requirement (an impostor naming a live exchange is refused),
drop-after-close,
result-then-exit collapsing to one result,
`failed{exit}` / `failed{timeout}` synthesis with the later exit deduped,
`failed{launch}` synthesis closing the exchange,
the job exchange — worker bound up front, started progress delivered and tapped, exit-0 closing with `ok{output_tail}`, failing exit / timeout / launch failure falling to the worker synthesis —
expect opening the exchange with the channel handed to `ready`,
expected-peer routing with a post-result `on_gone` cleaning up,
`failed{disconnected}` synthesis,
the host-anchored root exchange (observer-only delivery, the channel un-gated by the root's result, silent closure on exit),
the delivery tap — rule-1 + synthesized `failed` observed,
`source=None` on launch failure,
handler replies and denials untapped — root exposure,
the `Broker.run` root-exit / `stop` path,
and one real-loop job round-trip — a live-channel requester answered started-progress-then-derived-result over the real `Runtime` and transport),
and `client_test.py` + `cli_test.py` (the `Client` and the `broker` CLI over a real `UnixServerTransport`↔`UnixClientTransport` socket round-trip:
`from_env` set/unset,
the worker-side `progress`/`result` emission,
request correlation with uncorrelated arrivals set aside,
`call` surfacing progress and returning the correlated result under one whole-call deadline,
`await_any` returning the first correlated message,
`send` returning the client-minted request + `await_reply` reattaching to it,
the `timeout_after_interim` re-arm (a result outliving the initial bound / silence caught at a tighter one),
timeout / channel-close errors,
the `close(confirm=True)` handshake returning only after the host consumed everything sent,
the cross-thread close-abort of a blocked wait,
the CLI's inert no-channel path,
args validation,
and stdout wire-JSON output),
and `broxy_test.py` (a live `Broxy` between real local clients and a real upstream transport:
round-trip and `from_env` through the proxy,
progress-riding `call`,
the acceptance progress leaving the conversation pending,
sticky routing across concurrent connections,
detach→retain→claim replay in order with re-tagged correlation,
claim denials on unknown/collected ids and on live-waiter collisions (the wait-is-a-lock rule),
claim re-await of a detached route,
the check reports — unknown/malformed denied / pending with retained trail id + seq / ready replaying the unread window un-re-tagged before the report / collected after claim / live waiter undisturbed
— the cursor reads (post-delivery replay idempotence, read-marking spending the collect, the pending report, from-the-future and malformed `last_seen` denials),
mailbox byte-bound eviction (collected preferred, live waits exempt),
malformed-local-frame isolation,
clean-stop vs upstream-EOF exit codes,
and the `launch`/`serve`/`await` CLI edges).
All in `bro/local/run_tests.py`'s `PYTEST_FILES`.
No Docker.
`spawn.py`'s ports (marker + ABCs) are exercised by the adapters' own suites.
The live docker seam is covered from the consuming side by `ride/ride/e2e_test.py`;
the host process seam by `ride/ride/session_test.py`'s live ping round-trip.
