# trails/CLAUDE.md

Trails is the recording pipeline for bro runs: every `BaseBro.run()` / `.send()` ships its event stream (system prompt, user input, reasoning summaries, assistant text, tool calls/results, raw LLM payloads) to the deployed `trails-server`, where it becomes a *trail* — one recorded run — made of *steps* ordered by lulid (`base/lulid.py`). Recorded trails feed offline analysis, A/B comparison across specs, and forking (replay a prefix, continue differently). The canonical schema and design rationale live in the design doc on the `save bros logs` Flow task; this file covers the code, the deployed service, and the schema-evolution rules. Run any script with `--help` for flags.

## Architecture

```
  write  bro · HTTPTracker            read  trails CLI · TrailsClient
            │ sync per-step POSTs              │ GET /v1/trails…
            └────────────────┬─────────────────┘
                             ▼
                      trails-server        ECS Fargate · aiohttp · shared ALB
                             │
            ┌────────────────┼────────────────┐
            ▼                ▼                 ▼
     DynamoDB `trails`  DynamoDB         S3 `cw-trails-{account}`
     header + aggs      `trail_steps`    bodies ≥ 50KB (spillover)
                        lulid-keyed steps
```

- Write side lives in `llm/tracker.py` (`Tracker` ABC, `HTTPTracker`); plumbing through `BaseBro` and `ChatGPT` is documented in `bro/CLAUDE.md`. Writes are synchronous and crash-on-failure: `start_trail` fail-fast, `step` retries 100ms / 500ms / 2s then propagates, `end_trail` logs and never raises. Retries cover only transient failures (network errors, 5xx, 429); a deterministic 4xx (400 / 404 / 413) propagates immediately rather than sleeping through the schedule. The retried writes are idempotent: `HTTPTracker` mints each step's id client-side (a lulid) and reuses it across retries, so the server's conditional `attribute_not_exists(step_id)` Put turns a re-sent POST into a no-op — no duplicate step row, no double-counted token/step aggregate. The server auto-emits the `system_prompt` step inside trail creation.
- Recording is mandatory for bros: the default tracker factory raises when the `trails` secret is missing; `NullTracker` is opt-in (env-var kill switch `TRAILS_DISABLED=1`, tests via `conftest.py`, one-offs via `tracker=`).
- Read side is this package: `TrailsClient` for code, the `trails` CLI for humans, `fetch_recorded_trail` → `bro.fork.fork()` for forking.
- Bros never touch DynamoDB or S3 — only the server holds those credentials; clients hold one bearer token.

## Layout

- `client.py` — `TrailsClient` over the read endpoints (`list_trails` / `get_trail` / `get_steps` + `iter_*` cursor helpers); `default_client()` resolves the `trails` secret; `fetch_recorded_trail(client, trail_id)` rehydrates a header + steps into the `llm.tracker` dataclasses that `bro.fork.fork()` consumes — following the server's `{s3,url,size}` presigned-URL descriptor for any spilled body (via `resolve_body`, also exposed for callers that resolve selectively, like `call --resume`'s history extraction) so fork replay gets the full `llm_call.response.output` (the CLI's `list`/`show` keep the lazy descriptor)
- `cli.py` (`trails`) — `list` / `show` / `tree` / `fork` subcommands; counterpart to `sessions` / `rewind` for recorded bros
- `server/server.py` (`trails-server`) — aiohttp HTTP API: bearer-token auth middleware, request validation, storage exceptions → HTTP statuses
- `server/storage.py` — DynamoDB + S3 mechanics: step write + header-aggregate update are one `TransactWriteItems`; bodies ≥ 50KB spill to S3, > 10MB rejected with 413; reads resolve spilled bodies transparently (inline < 1MB, presigned URL above); floats convert to Decimal on write and back on read (DynamoDB numbers are Decimal)
- `server/` scripts — `deploy.sh`, `restart.sh`, `verify_deps.sh`, `run_local.sh`, `bootstrap_secrets.sh`; mirror `flow/focus/server/`

## Reader CLI

- `trails list [--bro | --parent] [--since --until --limit]` — newest first, paged through `$PAGER`; `--parent <trail_id>` lists a trail's forks
- `trails show <trail_id> [-f] [--interval <seconds>]` — header + step listing; each step line starts with the step's full id (that is the id `fork` takes), inline bodies truncate with `... <N more chars>`, spilled bodies render as size + URL. `-f`/`--follow` streams instead of paging: it keeps polling for new steps (`--interval` seconds apart) and renders them as they land, `tail -f`-style, exiting once the trail ends (the `end` step, or `ended_at` on the header for a trail that never got one); transient server errors are logged and retried on the next tick
- `trails tree <trail_id>` — walks parent pointers up to the root, then renders the full fork hierarchy
- `trails fork <trail_id> <step_id> [--initial <msg>] [--no-record]` — forks at the step (chaining through ancestor trails when the target is itself a fork) and drops into a `.send()` REPL. For continuing a `call` conversation prefer `call <bro> --resume` (`do/CLAUDE.md`), which picks the fork point and renders the history itself

The CLI keeps the parent's spec and prompt; for cross-model / cross-prompt forks call `bro.fork.fork(trail, step_id, llm_spec=…, system_prompt=…)` directly with a trail from `fetch_recorded_trail`. Fork-path selection (server-side `previous_response_id` vs client-side replay) is automatic — see `bro/fork.py`.

## Auth

Bearer token, mandatory by default; the no-auth escape hatch (`TRAILS_ALLOW_NO_AUTH=1`) requires a loopback `HOST` and is opt-in only. `server/run_local.sh` does **not** use it — it runs with bearer auth (`exec trails-server --allow-env` after exporting `TRAILS_BEARER_TOKEN` from the `trails` secret). The deployed token lives in SSM `/trails/bearer-token` (seeded by `server/bootstrap_secrets.sh`); clients resolve the `trails` secret (written by `setup/bootstrap_trails.sh` to `~/.ppp/trails.json`, see `setup/CLAUDE.md`) — read and write sides share that one credential.

## Deployment

ECS Fargate behind the shared ALB at `trails.<apex>`; CDK stacks `TrailsEcrStack` + `TrailsServerStack` in `infra/cdk/trails_stack.py` (stack table in `infra/CLAUDE.md`). Both DynamoDB tables and the S3 bucket are `RETAIN` — trails outlive the stack.

First-time ordering: `bootstrap_secrets.sh` → `deploy.sh` → `setup/bootstrap_trails.sh` on each client machine.

Recording is mandatory and crash-on-failure, so an unhealthy `trails-server` blocks every bro run — including the devoops bro that would deploy the fix. When the server itself is the thing that's broken, run the bro with `--no-trails` (e.g. `bro chat --no-trails devoops "deploy trails"`): it sets `TRAILS_DISABLED` in the container and drops the `trails` secret from the scoped set, so the rollout can't break the bro's own recording mid-deploy. `--no-trails` covers the containerized `bro run` / `bro chat` path and its aliases; an in-place run has no launch hop to set the env, so set `TRAILS_DISABLED=1` in the environment instead. Running `./trails/server/deploy.sh` directly (no bro) sidesteps recording entirely.

Server changes are not live until deployed. The unit suite fakes storage at the HTTP boundary, so a storage-layer change deserves a live re-smoke after deploy: `bro run dev 'list this dir'`, then `trails show <new id>` — the float→Decimal conversion was exactly the kind of gap the fakes miss.

## Schema evolution

The schema evolves additively, never destructively. New fields arrive optional; readers (`trails` CLI, `TrailsClient`, `bro.fork`) tolerate their absence on old rows; stored rows are never rewritten in place to change existing values (both tables are append-only with indefinite retention). The one sanctioned in-place write is an additive backfill that populates a newly-introduced optional attribute on old rows — idempotent, conditioned on the attribute's absence — when a new GSI needs it present to be complete (this is how the constant-PK `gsi_pk` behind the global newest-first list reached pre-existing rows). Such backfills (and one-off migrations like the `body`→`body_s3` spill-attribute move) ship as throwaway one-time scripts run right after the deploy and removed once done, so they don't live in the tree. Every header carries `bro_version` (= `configs.VERSION` at write time) — a change that cannot be expressed additively bumps `configs.VERSION` and keeps readers working for every prior version, keyed off that field. The reserved-for-v2 sub-bro fields (`parent.relationship='subagent'`, `entry_point='subagent'`, `tool_result.child_trail_id`) follow the same rule: v1 rows simply omit them.
