# bro/trails/CLAUDE.md

Trails is the universal registry and recording pipeline for LLM runs across harnesses. Readers and recorders use the `TrailsStore` facade; the `trails` credential selects either local filesystem storage or the deployed HTTPS service.

## Architecture

```text
bro · claude recorders                     readers
          │                                  │
          └────────── TrailsStore ────────────┘
                         │
              ┌──────────┴──────────┐
              ▼                     ▼
        LocalStore             TrailsClient
              │                     │ HTTPS
              ▼                     ▼
      local JSON/JSONL         trails-server
                                    │
                         DynamoDB + S3 storage
```

- `store.py` owns the client facade, store-neutral errors (`TrailNotFound`, `AppendConflict`, `TransientUnavailable`), common pagination helpers, recorded-trail rehydration, and `default_store()` dispatch. A `trails` config with no `backend` selects `service`; local storage is opt-in with `{"backend": "local"}`.
- `client.py` is the HTTPS service implementation. It maps not-found, append-conflict, and transient HTTP/transport failures onto the facade errors. Its recompute, check, and relink methods are service-only.
- `local.py` stores each trail under `<root>/trails/<id>/` as `header.json`, `steps.jsonl`, and optional `context.json`, with tool blobs under `<root>/trails/tools/<sha256>.json`. Appends are ordinal and `flock`-serialized, headers are atomically replaced, bodies remain inline, and listing scans trail directories while preserving the selector/cursor contract. A stale open header gets `end.inference = unreported` when read.
- The local root is `BRO_TRAILS_DIR` when set, otherwise `$XDG_DATA_HOME/bro` (default `~/.local/share/bro`). Container launch preparation bind-mounts that host root and sets `BRO_TRAILS_DIR` in the container whenever its hydrated `trails` credential selects local storage.
- `backends.py` is the harness seam. An adapter supplies `parse`, `classify`, `project`, `open`, and `validate_create`, plus its declared emitted message types; the registry is the complete harness dispatch surface.
- `rows.py` owns the shared aggregate fold, row construction, Claude row re-parsing, and message projection. Client-side stores and recorders do not import `bro.trails.server`.
- `server/storage.py` owns the DynamoDB/S3 implementation: conditional append transactions, indexes, S3 body spill, and UUID reads. `server/operations.py` owns recompute, check, and manifested relinking. These modules stay behind `TrailsClient`.
- **Recorder placement:** the shared write spine and every harness recorder live in `record/`; a recorder may import the facade and harness seam, never the reverse. A third harness adds `record/<harness>.py` over `spine.Recording` beside its adapter.
- Harness adapters mint lineage only when creating a trail. Writers cannot mutate an edge; the service operator can repair one through manifested `relink`, and service audits detect copied records across trails.
- Bro tool schemas are content-addressed and referenced by `tools_sha256` on a row; `model.tools_sha256` is the canonical digest helper.

Writer-reported outcomes use `end.reason`. Absence of a writer verdict is represented as `end.inference = unreported`, not as a failure verdict.

## Surfaces

- `model.py` owns the shared trail, step, lineage, and spill-descriptor vocabulary consumed by readers and recorders. A step id is an ordinal — position N in the trail's native record stream — in rows and `forked_from` / `summoned_by` pointers alike. `lineage.py` is the cycle-detecting root-first chain walker.
- `record/spine.py` owns recording creation, ordinal extent validation, batched appends, liveness, and ending; `record/bro.py` adapts `bro.llm.tracker.Tracker`, and `record/claude.py` records Claude transcripts.
- `POST /v1/trails` opens a service trail from `body.records`; `POST /v1/trails/{id}/records` appends records at `offset`. A committed retry returns the current extent without folding again; any other extent mismatch is a conflict.
- Service `/steps` returns the lossless native stream and `/messages` the generalized projection. UUID lookup, bounded UUID projection, and exact-row reads support Claude lineage recovery.
- Bro projection derives reasoning, assistant text, tool calls, and terminal assistant status from `llm_call.response.output`; rows of those decomposed kinds do not project separately.
- Service admin endpoints for recompute, check, and relink are aggregate repair, verification/audit, and lineage repair. The local backend intentionally exposes none of them.
- Header responses expose provider-raw usage by model. Provider normalization belongs to the provider-aware usage layer, not the harness adapter.
- List queries accept exactly one selector — `harness`, `bro`, or `forked_from` — plus the common time range and opaque cursor.
- `rewind.py` (`rewind`) works through `TrailsStore` for either backend: `show` and `grep` render projected messages across a fork chain, `steps` renders one native stream, and `list` / `tree` navigate headers and lineage.

## Service auth

Bearer auth is mandatory outside an explicit loopback-only `TRAILS_ALLOW_NO_AUTH=1` service run. The `trails` credential schemas are documented in `bro/setup/CLAUDE.md`.
