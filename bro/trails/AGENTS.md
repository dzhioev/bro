# bro/trails/AGENTS.md

Trails is the universal registry and recording pipeline for LLM runs across harnesses. Every run has one header in the `trails-v2` DynamoDB table and one body in the shared ordinal `trail_steps_v2` table. The deployed `trails-server` is the only component with DynamoDB/S3 access; clients use the shared bearer-token secret.

## Architecture

```text
bro · claude recorder                       readers
          │                                  │
          └──────── HTTPS ───────┬───────────┘
                                 ▼
                         trails-server
                                 │
               ┌─────────────────┴─────────────────┐
               ▼                                   ▼
       DynamoDB `trails-v2`          DynamoDB `trail_steps_v2`
       universal headers             ordinal native records
                                                 │
                                                 ▼
                                      S3 body spill + tool blobs
```

- `bro/trails/server/storage.py` owns headers, the extent-conditional append protocol, ordinal storage and spillover, UUID projections and point reads, content-addressed tool blobs, list indexes, and unreported-run inference; `folding.py` is the shared aggregate fold.
- `bro/trails/server/operations.py` owns recompute, check (including billing and cross-trail UUID audits), and manifested relinking.
- `bro/trails/server/backends.py` is the harness seam. An adapter supplies exactly `parse`, `classify`, `project`, `open`, and `validate_create`, plus its declared emitted message types; the registry is the complete harness dispatch surface.
- **Recorder placement:** the shared write spine and every harness recorder live in `bro/trails/record/`; a recorder may import the seam it rides, never the reverse. A third harness adds `record/<harness>.py` over `spine.Recording` beside its server adapter, not recording machinery in `bro/llm/`, `bro/trails/client.py`, or the harness package.
- Harness adapters mint lineage only when creating a trail. Writers cannot mutate an edge; operators repair a missing edge through manifested `relink`, and audits detect copied records across trails.
- `trail_steps_v2` uses `(trail_id S, step_id N)` plus a keys-only UUID index for Claude lineage lookup. A header names its body store in `body_storage` and carries its current `extent`; append transactions condition on that extent.
- Bodies at least 50 KB spill to S3. Bro tool schemas are content-addressed under `bro/trails/tools/{sha256}.json` and referenced by `tools_sha256` on a row; `bro.trails.model.tools_sha256` is the canonical digest helper for clients.
- Launch context is a harness-neutral attachment under `bro/trails/{id}/context.json`.

Writer-reported outcomes use `end.reason`; the stale-run sweep instead records `end.inference = unreported`, so absence of a writer verdict is not presented as a failure verdict.

## Surfaces

- `bro/trails/model.py` owns the shared trail, step, lineage, and spill-descriptor vocabulary consumed by readers and recorders. A step id is an ordinal — position N in the trail's native record stream — in a row and in a `forked_from` / `summoned_by` pointer alike, so pointers order against rows directly; a pointer may also carry an event index. `bro/trails/lineage.py` is the cycle-detecting root-first chain walker.
- `bro/trails/client.py` owns the persistent authenticated HTTPS transport. `TrailsClient` exposes paged headers, native steps, generalized messages, launch context, universal append, and admin operations.
- `bro/trails/display/` owns the typed process-local display records, immutable scenario presets, stateful presentation core, renderer-neutral block operations, the live and recorded adapters, plain stream/retained and lazy Rich-panel terminal renderers, and the lazy embedded Textual trail view. The Textual renderer owns selectable message/reasoning/tool/status widgets and logical-line copy reflow; chat launch code supplies only the app shell and lifecycle. The recorded adapter validates `/messages`, adapts headers/context/native steps/navigation rows, and collects exact fork-bounded segments. It is a view layer only; none of its records enter trail storage or server contracts.
- `bro/trails/record/spine.py` owns recording creation, ordinal extent validation, batched appends, liveness, and ending; `record/bro.py` adapts `bro.llm.tracker.Tracker`, and `record/claude.py` (`bro.trails.record.claude`) records Claude transcripts.
- `POST /v1/trails` opens the body from `body.records`.
- `POST /v1/trails/{id}/records` sends records beginning at `offset`. A committed retry returns the current extent without folding again; any other extent mismatch is a conflict.
- `GET /v1/trails/{id}/steps` returns the lossless native stream. `GET /v1/trails/{id}/messages` returns the generalized projection; billing usage is read from the row selected at append time. `GET /v1/steps?uuid=…` returns matching row identities, `/steps/uuids` returns a bounded UUID projection, and `/steps/{step_id}` returns one exact row.
- Bro projection derives reasoning, assistant text, tool calls, and terminal assistant status from `llm_call.response.output`; rows of those decomposed kinds do not project separately.
- `POST /v1/admin/trails/{id}/recompute`, `/v1/admin/trails/check`, and `/v1/admin/trails/{id}/relink` are the aggregate repair, non-mutating verification/audit, and manifested lineage-repair surfaces. The store-wide check keeps its long request alive with JSON-whitespace heartbeats and ends with one verdict object.
- Header responses expose provider-raw usage by model. Provider normalization belongs to the provider-aware usage layer, not the harness adapter.
- List queries accept exactly one indexed selector: `harness`, `bro`, or `forked_from`, plus the common time range and cursor.
- `bro/trails/rewind.py` (`rewind`) is the reader CLI for every harness: it owns argument parsing, queries, follow polling, regex matching, and grep context while every `show`, `steps`, `list`, `tree`, and `grep` record renders through the matching display preset.

## Auth

Bearer auth is mandatory outside an explicit loopback-only `TRAILS_ALLOW_NO_AUTH=1` run. Clients configure the server URL and bearer token through the `trails` secret documented in `bro/setup/AGENTS.md`.
