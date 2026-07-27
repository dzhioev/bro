# trails/CLAUDE.md

Trails is the universal registry and recording pipeline for LLM runs across harnesses. Every run has one header in the `trails-v2` DynamoDB table; migrated bodies use the shared ordinal `trail_steps_v2` table, and the server dual-reads legacy bodies until their retirement stage. The deployed `trails-server` is the only component with DynamoDB/S3 access; clients use the shared bearer-token secret.

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

- `trails/server/storage.py` owns headers, the extent-conditional append protocol, ordinal storage and spillover, dual reads, content-addressed tool blobs, list indexes, and lost-run sweeping; `folding.py` is the shared aggregate fold.
- `trails/server/operations.py` owns recompute, check (including billing and cross-trail UUID audits), and manifested relinking.
- `trails/server/backends.py` is the harness seam. An adapter supplies exactly `parse`, `classify`, `project`, `open`, and `validate_create`, plus its declared emitted message types; the registry is the complete harness dispatch surface.
- `trail_steps_v2` uses `(trail_id S, step_id N)`. A migrated header is identified by `body_storage = trail_steps_v2` and carries its current `extent`; append transactions condition on that extent.
- Bodies at least 50 KB spill to S3. Bro tool schemas are content-addressed under `trails/tools/{sha256}.json` and referenced by `tools_sha256` on a row; `trails.model.tools_sha256` is the canonical digest helper for clients and migrations.
- Launch context is a harness-neutral attachment under `trails/{id}/context.json`.

## Transition

The legacy sources remain readable until the final retirement stage: bro rows in `trail_steps`, and Claude JSONL at `trails/claude/{id}/records.jsonl`. Reads select `trail_steps_v2` only after the header's migrated marker is set, so body migrations can write and verify a complete target before switching one trail.

The legacy `POST /steps`, `PUT /artifact`, and client aggregate updates accept only unmigrated trails. They remain as compatibility writers while live clients move to `POST /records`; migrated trails reject them, and universal headers accept no client-written usage or turn totals.

## Surfaces

- `trails/model.py` owns the shared trail, step, lineage, and spill-descriptor vocabulary consumed by readers and recorders. Lineage step ids admit legacy strings or universal ordinals, and pointers may carry an event index.
- `trails/client.py` owns the persistent authenticated HTTPS transport. `TrailsClient` exposes paged headers, native steps, generalized messages, launch context, universal append, and admin operations; its `HTTPTracker` remains the bro compatibility writer until its client migration stage. The Claude compatibility recorder remains in `session_log/recorder.py`.
- `POST /v1/trails` opens a legacy body for the old `system_prompt` / `artifact` envelopes, or a universal body when `body.records` is present.
- `POST /v1/trails/{id}/records` sends records beginning at `offset`. A committed retry returns the current extent without folding again; any other extent mismatch is a conflict.
- `GET /v1/trails/{id}/steps` returns the lossless native stream. `GET /v1/trails/{id}/messages` returns the generalized projection; billing usage is read from the row selected at append time.
- Bro projection derives reasoning, assistant text, tool calls, and terminal assistant status from `llm_call.response.output`; decomposed legacy rows do not project separately.
- `POST /v1/admin/trails/{id}/recompute`, `/v1/admin/trails/check`, and `/v1/admin/trails/{id}/relink` are the aggregate repair, non-mutating verification/audit, and manifested lineage-repair surfaces.
- Header responses expose provider-raw usage by model. Provider normalization belongs to the provider-aware usage layer, not the harness adapter.
- List queries accept exactly one indexed selector: `harness`, `bro`, or `forked_from`, plus the common time range and cursor.
- `trails/rewind.py` (`rewind`) is the reader CLI for every harness: `show` and `grep` render the shared `/messages` conversation across its fork chain; `steps` renders one trail's native `/steps` debugging view; `list` and `tree` navigate headers and lineage.

## Auth and deployment

Bearer auth is mandatory outside an explicit loopback-only `TRAILS_ALLOW_NO_AUTH=1` run. The deployed token lives in SSM `/trails/bearer-token`; `trails/bootstrap.sh` writes the client secret.

The ECS service and its retained header, legacy-step, universal-step, and bucket resources are defined in `infra/cdk/trails_stack.py`.

The historical Claude backfill produced 1,119 trails with `version = 'legacy-session-log'`; its manifests remain under `trails/migrations/` in the trails bucket.

Server and CDK changes are not live until `trails/server/deploy.sh` builds the image and deploys the trails stacks. The unit suite fakes AWS boundaries, so a storage change requires a post-deploy append/read/check smoke in addition to tests.
