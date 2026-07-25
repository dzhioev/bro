# trails/CLAUDE.md

Trails is the universal registry and recording pipeline for LLM runs across harnesses. Every run has one header in the `trails-v2` DynamoDB table; its lossless body stays in harness-native storage behind the server-only backend seam. The deployed `trails-server` is the only component with DynamoDB/S3 access; clients use the shared bearer-token secret.

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
       DynamoDB `trails-v2`                 harness body backends
       universal headers                    bro: `trail_steps` + S3 spill
                                             claude: one S3 JSONL suffix
```

- `trails/server/storage.py` owns universal headers, immutable-field enforcement, list indexes, lost-run sweeping, backend dispatch, and serve-time `usage` / `models` projection.
- `trails/server/backends.py` owns the `BodyBackend` seam and the cached `BroBackend` / `ClaudeBackend` implementations: body open/write, native-record iteration, generalized message projection, and provider-raw usage access.
- Bro bodies retain the `trail_steps` table and the existing `trails/{id}/steps/{step_id}.json` spillover layout.
- Claude bodies use `trails/claude/{id}/records.jsonl`; optional launch context uses `trails/claude/{id}/launch-context.json`. Artifacts are complete suffix snapshots replaced atomically with S3 PUT. Native step ids are decimal line indexes; invalid and blank lines remain addressable and are returned with their raw text.
- Header migrations write reports under `trails/migrations/bro-header-v2/` in the trails bucket.

## Surfaces

- `llm/tracker.py` is the bro write client. `HTTPTracker` creates a `harness='bro'` trail, appends client-idempotent steps, keeps it alive, and ends it with `ok | raised | error`; the server alone stamps `lost`.
- `trails/client.py` is the synchronous client: paged headers, native steps, and generalized messages through `iter_trails`, `iter_steps`, and `iter_messages`, plus the claude recorder's write surface (`create_trail`, `replace_artifact`, `update_header`, `end_trail`, `keepalive`) and `get_launch_context`. The claude recorder itself is `session_log/recorder.py`.
- `GET /v1/trails/{id}/steps` returns the backend's lossless native records. `GET /v1/trails/{id}/messages` returns generalized events and accepts repeated `type` query parameters; a claude message id split across records bills its `llm_call` once. `GET /v1/trails/{id}/context` returns the stored launch-context document.
- `POST /v1/trails` creates a header and opens its body; a Claude create may include `body.launch_context`. `PUT /v1/trails/{id}/artifact` replaces a Claude snapshot. `PATCH /v1/trails/{id}` accepts only the live mutable header/native fields. `POST /v1/trails/{id}/end` finalizes a run.
- Header responses carry stored fields plus computed `usage` and `models`. Provider-raw per-model counters in `native.usage` are the source of truth.
- List queries accept exactly one indexed selector: `harness`, `bro`, or `forked_from`, plus the common time range and cursor.
- `trails/rewind.py` (`rewind`) is the reader CLI for every harness: `list`, harness-aware `show` (bro step listing; claude fork-chain conversation render, `-f` follow), `grep`, `tree`. Ids the server doesn't know fall back to the legacy session-log reader until the historical backfill retires it.

## Auth and deployment

Bearer auth is mandatory outside an explicit loopback-only `TRAILS_ALLOW_NO_AUTH=1` run. The deployed token lives in SSM `/trails/bearer-token`; `trails/bootstrap.sh` writes the client secret.

The ECS service is defined in `infra/cdk/trails_stack.py`. Both header tables, `trail_steps`, and the bucket use `RETAIN`. The stack keeps the legacy `trails` table beside `trails-v2` for cutover and grants the task role both; `TRAILS_HEADER_TABLE` selects the task's active table at CDK synth, defaulting to `trails-v2`.

Header cutover order:

1. Run `trails/server/prepare_header_table.sh` to add `trails-v2` while the existing image and task definition still use `trails`.
2. Run `trails-migrate-headers --bucket cw-trails-<account> --dry-run`, inspect the report, then run it without `--dry-run` for the bulk copy.
3. Freeze new bro writes and wait for every active bro trail to end; no bro run may straddle the switch.
4. Run the same migration command again for the idempotent delta pass.
5. Deploy every trails-server task from the landed revision with recording disabled for the deployment run (`--no-trails` / `TRAILS_DISABLED=1`). The default CDK selection points the tasks at `trails-v2`.
6. Verify `/health`, header reads, a new bro run, native steps, and generalized messages before releasing the write freeze. Keep the legacy table intact.

Server changes are not live until deployed. The unit suite fakes AWS boundaries, so a storage change requires a post-deploy run/read smoke in addition to tests.
