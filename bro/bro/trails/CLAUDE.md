# trails/CLAUDE.md

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

- `trails/server/storage.py` owns headers, the extent-conditional append protocol, ordinal storage and spillover, UUID projections and point reads, content-addressed tool blobs, list indexes, and unreported-run inference; `folding.py` is the shared aggregate fold.
- `trails/server/operations.py` owns recompute, check (including billing and cross-trail UUID audits), and manifested relinking.
- `trails/server/backends.py` is the harness seam. An adapter supplies exactly `parse`, `classify`, `project`, `open`, and `validate_create`, plus its declared emitted message types; the registry is the complete harness dispatch surface.
- **Recorder placement:** the shared write spine and every harness recorder live in `trails/record/`; a recorder may import the seam it rides, never the reverse. A third harness adds `record/<harness>.py` over `spine.Recording` beside its server adapter, not recording machinery in `llm/`, `trails/client.py`, or the harness package.
- Harness adapters mint lineage only when creating a trail. Writers cannot mutate an edge; operators repair a missing edge through manifested `relink`, and audits detect copied records across trails.
- `trail_steps_v2` uses `(trail_id S, step_id N)` plus a keys-only UUID index for Claude lineage lookup. A header names its body store in `body_storage` and carries its current `extent`; append transactions condition on that extent.
- Bodies at least 50 KB spill to S3. Bro tool schemas are content-addressed under `trails/tools/{sha256}.json` and referenced by `tools_sha256` on a row; `trails.model.tools_sha256` is the canonical digest helper for clients and migrations.
- Launch context is a harness-neutral attachment under `trails/{id}/context.json`.

Writer-reported outcomes use `end.reason`; the stale-run sweep instead records `end.inference = unreported`, so absence of a writer verdict is not presented as a failure verdict.

## Manifested operations

An operation that drives the store from outside the server manifests what it will do before doing it, and its `plan` produces that enumeration without touching anything. Both live in `migrations/` over the shared direct-AWS I/O in `migrations/direct.py`, and both need AWS credentials the ordinary client tier does not carry.

`trails-retire-legacy-stores` (`migrations/retirement.py`): `plan` enumerates every table, object and header field the retirement destroys and runs its preconditions — every trail universal, each authorising report clean, neither legacy table written since the soak window opened — and `apply` manifests that enumeration before deleting, then verifies. Its manifest is `trails/retirement/manifest.json` in the trails bucket, deliberately outside the `trails/migrations/` prefix the retirement deletes, and it is the single account of what the retirement removed: the tables and their item counts, every object key, the stripped header fields with their prior values, and the authorising reports it read. Because `plan` reads prefixes the run then deletes, a resumed `apply` works from the stored manifest rather than re-planning.

`trails-normalise-lineage-ordinals` (`migrations/lineage_ordinals.py`): rewrites lineage pointers whose `step_id` is the decimal string of its ordinal. Each `apply` writes the round it is about to perform under `trails/migrations/lineage-ordinals/rounds/`, rewrites conditionally on the string that round recorded, and ends in a `verify` that re-reads every header; the rounds are append-only because nothing here destroys evidence the store cannot show again, so a partial run is finished by planning a fresh round rather than resuming a stored one.

## Surfaces

- `trails/model.py` owns the shared trail, step, lineage, and spill-descriptor vocabulary consumed by readers and recorders. A step id is an ordinal — position N in the trail's native record stream — in a row and in a `forked_from` / `summoned_by` pointer alike, so pointers order against rows directly; a pointer may also carry an event index. `trails/lineage.py` is the cycle-detecting root-first chain walker.
- `trails/client.py` owns the persistent authenticated HTTPS transport. `TrailsClient` exposes paged headers, native steps, generalized messages, launch context, universal append, and admin operations.
- `trails/record/spine.py` owns recording creation, ordinal extent validation, batched appends, liveness, and ending; `record/bro.py` adapts `llm.tracker.Tracker`, and `record/claude.py` (`trails.record.claude`) records Claude transcripts.
- `POST /v1/trails` opens the body from `body.records`.
- `POST /v1/trails/{id}/records` sends records beginning at `offset`. A committed retry returns the current extent without folding again; any other extent mismatch is a conflict.
- `GET /v1/trails/{id}/steps` returns the lossless native stream. `GET /v1/trails/{id}/messages` returns the generalized projection; billing usage is read from the row selected at append time. `GET /v1/steps?uuid=…` returns matching row identities, `/steps/uuids` returns a bounded UUID projection, and `/steps/{step_id}` returns one exact row.
- Bro projection derives reasoning, assistant text, tool calls, and terminal assistant status from `llm_call.response.output`; rows of those decomposed kinds do not project separately.
- `POST /v1/admin/trails/{id}/recompute`, `/v1/admin/trails/check`, and `/v1/admin/trails/{id}/relink` are the aggregate repair, non-mutating verification/audit, and manifested lineage-repair surfaces. The store-wide check keeps its long request alive with JSON-whitespace heartbeats and ends with one verdict object.
- Header responses expose provider-raw usage by model. Provider normalization belongs to the provider-aware usage layer, not the harness adapter.
- List queries accept exactly one indexed selector: `harness`, `bro`, or `forked_from`, plus the common time range and cursor.
- `trails/rewind.py` (`rewind`) is the reader CLI for every harness: `show` and `grep` render the shared `/messages` conversation across its fork chain; `steps` renders one trail's native `/steps` debugging view; `list` and `tree` navigate headers and lineage.

## Auth and deployment

Bearer auth is mandatory outside an explicit loopback-only `TRAILS_ALLOW_NO_AUTH=1` run. The deployed token lives in SSM `/trails/bearer-token`; `trails/bootstrap.sh` writes the client secret.

The ECS service and its retained header, step, and bucket resources are defined in `infra/cdk/trails_stack.py`.

The historical Claude backfill produced 1,119 trails with `version = 'legacy-session-log'`; its manifests remain under `trails/migrations/` in the trails bucket.

Server and CDK changes are not live until `trails/server/deploy.sh` builds the image and deploys the trails stacks. The unit suite fakes AWS boundaries, so a storage change requires a post-deploy append/read/check smoke in addition to tests.
