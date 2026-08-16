# bro/trails/CLAUDE.md

Trails is the universal registry and recording pipeline for LLM runs across harnesses. Recorders and readers use the synchronous `TrailsStore` contract; the `trails` credential selects its concrete backend, and a process without that credential records locally.

## Architecture

```text
bro · claude recorders                     readers
          │                                  │
          └────────── TrailsStore ────────────┘
                         │
              ┌──────────┴──────────┐
              ▼                     ▼
         LocalStore             NetworkStore
              │                     │ HTTP(S)
              ▼                     ▼
      local JSON/JSONL         trails-server
                                    │ off-loop dispatch
                                    ▼
                               TrailsStore
                              ┌─────┴──────┐
                              ▼            ▼
                         LocalStore   DynamoStore
                                      │
                                 DynamoDB + S3
```

- `store.py` owns `TrailsStore`, store-neutral errors (`TrailNotFound`, `AppendConflict`, `TransientUnavailable`), pagination helpers, recorded-trail rehydration, and credential dispatch. `resolve_config(store)` is where a process's backend is decided — the `trails` credential where it resolves, `local` where it does not, so configuring the credential is what opts a deployment into the service or dynamo backends; `selects_local_storage` is the predicate the launch layer asks of a scope it is composing a container for. Within a config, a missing `backend` selects `service`; `local` and `dynamo` are explicit. The `dynamo` branch lazy-imports the server package.
- `network.py` owns `NetworkStore`, the authenticated wire proxy. HTTPS is required except for HTTP loopback hosts. It maps not-found, append-conflict, unsupported-operation, and transient transport failures onto the store errors and owns operation-specific retry schedules. A 404 becomes `TrailNotFound` only when its body reports the missing trail (`model.trail_not_found_body` is the shape both sides read); every other 404 surfaces as `HTTPStatusError` carrying what the response said. Its concrete `recompute`, `check`, and `relink` methods forward the Dynamo administration endpoints; they are not part of `TrailsStore`.
- `claude_lineage.py` owns the claude evidence contract and resolves fork lineage from it: the adopted segment, its lines' record uuids and digests, and the sibling segments sharing those records. It reads rows through `LineageIndex`, the store-internal surface each backend implements its own way (`LocalStore` filters headers by segment before touching a row; `DynamoStore` queries its keys-only uuid index), and returns a fork point with the file ranges the new trail owns, a decline while a history copy is mid-write, or a root. Nothing on this path crosses the wire, and continuity across recorder lifetimes comes from the stored rows alone.
- `local.py` stores each trail under `<root>/trails/<id>/` as `header.json`, `steps.jsonl`, and optional `context.json`, with tool blobs under `<root>/trails/tools/<sha256>.json`. Appends are ordinal and `flock`-serialized, headers are atomically replaced, bodies remain inline, and listing preserves the selector/cursor contract. A stale open header gets `end.inference = unreported` when read.
- The local root is `bro.workspace.paths.trails_dir` under the project root — one per repository. `bro.launch.trails` contributes its mount to the `Launch` composed by the cw-session and bro-run launch surfaces, binding the host root where a container's own resolution already looks.
- `TRAILS_DISABLED` (presence-checked) turns a process's recording off, since a backend now resolves for every run. It is the recovery path: deploying or repairing `trails-server` through a bro whose own recording would otherwise go through it. Launch surfaces spell it `--no-trails`; an in-place run sets the variable in its shell.
- `server/server.py` is an aiohttp proxy over any configured `TrailsStore`; every synchronous store call runs through `asyncio.to_thread`. The process resolves its hosted store with `default_store()`. `/v1/admin/*` is mounted on every server and answers 501 where the hosted store has no administration surface; the unreported-trail sweep starts only for a `DynamoStore`.
- `server/dynamo.py` owns `DynamoStore(TrailsStore)`: conditional append transactions, indexes, S3 body spill/resolution, UUID reads, and its store-owned thread pool for UUID-query and spilled-row fan-outs. `server/dynamo_types.py` owns Dynamo conversion and row constants. `server/operations.py` remains the recompute/check engine and owns manifested relinking.
- Stored rows are served rows. Claude message projection reparses the row's `body`; reads do not add `raw` or `record`. Dynamo's `body_s3` fields are resolved back to inline `body` values before a row leaves the store.
- `backends.py` is the harness seam. An adapter supplies `parse`, `classify`, `project`, `open`, `validate_create`, and optionally `resolve_lineage`, plus its declared emitted message types; the registry is the complete harness dispatch surface. Reads are harness-neutral: `blaze` is the one store method taking harness-specific data, in its own `native`, `body`, and `lineage` arguments.
- `display/` owns the typed process-local display records, immutable scenario presets, stateful presentation core, renderer-neutral block operations, the live and recorded adapters, plain stream/retained and lazy Rich-panel terminal renderers, and the lazy embedded Textual trail view. The recorded adapter validates `/messages`, adapts headers/context/native steps/navigation rows, and collects exact fork-bounded segments. It is a view layer only; none of its records enter trail storage or server contracts.
- `rows.py` owns aggregate folding, row construction, and message projection. Client-side stores and recorders do not import `bro.trails.server`.
- **Recorder placement:** the shared write spine and every harness recorder live in `record/`; a recorder may import the store contract and harness seam, never the reverse. Both harness-specific classes deliberately remain named `Recorder`.
- Harness adapters mint lineage only when blazing a trail, and a trail's step ids are its rows' line ordinals in local storage. Writers cannot mutate an edge; the Dynamo operator can repair one through manifested `relink`, and audits detect copied records across trails.
- Bro tool schemas are content-addressed and referenced by `tools_sha256` on a row; `model.tools_sha256` is the canonical digest helper.

Writer-reported outcomes use `end.reason`. Absence of a writer verdict is represented as `end.inference = unreported`, not as a failure verdict.

## Surfaces

- `model.py` owns `BlazeRequest`, shared validation constants, trail/step/lineage records, and body helpers. `BlazeRequest.from_wire()` / `to_wire()` is the one blaze-envelope validator; harness-native validation and body opening remain in each store. A step id is an ordinal in rows and lineage pointers.
- `record/spine.py` owns blaze, ordinal extent validation, batched appends, liveness, and ending; `record/bro.py` adapts `bro.llm.tracker.Tracker`, and `record/claude.py` records Claude transcripts, reporting each adopted segment's evidence and applying the verdict it gets back.
- `POST /v1/trails` blazes from `body.records`, resolving `lineage` evidence when the request carries it — the response then adds the verdict (`adopted`, `forked_from`, `chunks`) and a declined adoption creates nothing. `POST /v1/trails/{id}/records` appends at `offset`. A committed retry returns the current extent without folding again; any other extent mismatch is a conflict.
- `/steps` returns the native stream and `/messages` its generalized projection. Large bodies remain inline over the wire.
- `GET /v1/trails/{id}/context` returns `{"launch_context": null}` for an existing trail without context and 404 only when the trail is missing.
- Bro projection derives reasoning, assistant text, tool calls, and terminal assistant status from `llm_call.response.output`; rows of those decomposed kinds do not project separately.
- Header responses expose provider-raw usage by model. Provider normalization belongs to the provider-aware usage layer, not the harness adapter.
- List queries accept exactly one selector — `harness`, `bro`, or `forked_from` — plus the common time range and opaque cursor.
- `rewind.py` (`rewind`) is the reader CLI for every harness, working through `TrailsStore`: it owns argument parsing, queries, follow polling, regex matching, and grep context, while every `show`, `steps`, `list`, `tree`, and `grep` record renders through the matching display preset. The text views accept `--output-offset` / `--output-limit` for bounded windows.
- `contract_test.py` runs the same contract suite against `LocalStore` and `NetworkStore` over a real loopback aiohttp server backed by `LocalStore`; `claude_lineage_test.py` drives the resolver over a real store, and `record/claude_test.py` drives the recorder over one. `network_test.py` owns transport/retry/error mapping; `server/dynamo_test.py` owns fake-backed Dynamo mechanics.

## Service auth

Bearer auth is mandatory outside an explicit loopback-only `TRAILS_ALLOW_NO_AUTH=1` server run. The hosted backend comes from `trails.json`; auth remains in `--trails-bearer-token` / `TRAILS_BEARER_TOKEN` and `--trails-allow-no-auth` / `TRAILS_ALLOW_NO_AUTH`. Credential schemas are documented in `bro/setup/CLAUDE.md`.
