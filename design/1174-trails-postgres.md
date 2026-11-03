# Design: the hosted trails store on PostgreSQL

This file is the specification of [#1174](https://github.com/dzhioev/bro/issues/1174).
It lives on the branch `design/1174-trails-postgres`, which is never merged:
the pull request that carries it is a design review only.

## 1. Goal, scope and constraints

Goal: the hosted trails store moves from DynamoDB and S3 to PostgreSQL.
A `PostgresStore` implements the `TrailsStore` contract against any PostgreSQL database;
the repository's hosted deployment runs it on Aurora PostgreSQL Serverless v2, a writer plus a standby reader in a second availability zone.
`DynamoStore`, its S3 spill, and its administration code are deleted once the hosted data has moved.

What it enables, for example:

- An append is one transaction:
  its rows, the header's new extent and the header's new fold become visible together or not at all, at any batch size.
- A tool result of several megabytes is stored inline in its row;
  nothing spills to object storage, and no body limit applies below the server's 64 MiB request cap.
- A new access pattern is an index added by a schema revision (§9), not a secondary index that had to be planned before the table existed.
- [#1166](https://github.com/dzhioev/bro/issues/1166)'s message search can keep its index table current inside the transaction of each append.
- A consumer points a trails-server at any PostgreSQL with a password in the `trails` credential;
  IAM token login is an option for Aurora, never a requirement.

The owner settled the design's decisions on 2026-11-03;
§17 records each one and the section that applies it.

Out of scope, each with where it belongs:

- the message-search index and the `rewind grep` rework ([#1166](https://github.com/dzhioev/bro/issues/1166)), which this design only enables;
- making the native recorder resilient to a failed trail write ([#1189](https://github.com/dzhioev/bro/issues/1189));
- serving reads from the standby, connection proxies, monitoring and alarms, and automated restores;
- the race in which a fork is minted while its parent is being deleted, which every backend has today and this design leaves as it is.

Constraints:
core imports no other workspace member, and client-side stores and recorders never import `bro.trails.server`;
framework code stays consumer-neutral, so nothing in `PostgresStore` assumes AWS beyond its optional IAM login;
recorders and `rewind` keep their behaviour, and the server's routes change only as §10.1 lists;
backward compatibility is not a constraint (`AGENTS.md`, "Conventions");
a violated assumption fails fast (the development style policy).

## 2. Concepts

Terms the repository already defines, as this design uses them:

- **Trail** — one recorded run:
  a **header** (identity, lifecycle facts, and the aggregate folded from its rows) and an ordered stream of **rows**, the row at ordinal `n` carrying `step_id` `n`.
- **Extent** — the header's count of stored rows;
  an append names the `offset` it expects the extent to be.
- **Fold** — the header fields derived from the rows:
  `extent`, `turn_count`, `last_billed_message_id`, and the server-derived native fields `usage`, `step_counts_by_kind` and `lineage_head` (`rows.py`, `backends.SERVER_DERIVED_NATIVE_FIELDS`).
- **Trail format** — the schema version a stored header or row carries in `format`;
  reads upgrade an older record in memory (`formats.py`).
  A **trail-format migration** rewrites a trail's stored records into the current format, through `migrate_trail` and before an append, an attach or a recompute.
- **Tool blob** — the tool definitions of a native `llm_call`, stored once under the SHA-256 of their canonical JSON and named by `tools_sha256` on the rows that used them.
- **Import mark** — the `importing` field an imported header carries until its import is sealed (`importing.py`).
- **Lineage index** — the two lookups a trail format's lineage resolver makes through the store:
  the trails recording any of a set of segments, and whether any of a set of trails stores a record uuid.

Terms this design introduces:

- **Document** — a header or a row exactly as stored:
  a JSON object with its body inline and its trail format as written.
- **Derived column** — a table column the store computes from the upgraded document at every write of that document;
  keys, indexes and filters read it, and nothing serves it.
- **Served header** — the header a read returns:
  the upgraded header document plus the read projections of §6.1.
- **Trail transaction** — an exclusive unit of work over one trail.
  No other trail transaction on the same trail runs while it is open, and what it writes becomes visible to readers when it closes (§4.1).
- **Storage primitives** — the operations a backend supplies to the shared contract implementation (§4.2).
- **Schema revision** — one numbered change to the PostgreSQL schema, shipped as a SQL file and applied by the store when it opens (§9).
  It is not a trail-format migration, which rewrites documents.
- **Hosted store** — the store the deployed trails-server serves, as its `trails` credential selects.
- **Switch revision** — the revision of the trails stack whose store configuration names PostgreSQL instead of DynamoDB (§11.4, §12).
- **The window** — the quiesced period in which the hosted data moves (§12.3).

## 3. Architecture

```text
bro · claude recorders                         readers
          │                                       │
          └────────────── TrailsStore ─────────────┘
                               │
                ┌──────────────┴───────────────┐
                ▼                              ▼
       TransactionalStore                NetworkStore
        ┌───────┴─────────┐                    │ HTTP(S)
        ▼                 ▼                    ▼
   LocalStore       PostgresStore         trails-server
        │                 │                    │ off-loop dispatch
  local JSON/JSONL    PostgreSQL               ▼
                                      LocalStore or PostgresStore
```

- `bro/trails/transactional.py`, in core, holds `TransactionalStore(TrailsStore)`:
  every contract operation, written once over the storage primitives (§4).
- `bro/trails/local.py` keeps `LocalStore`, now the `TransactionalStore` whose primitives are files under the local root (§6.2).
- `bro/trails/server/postgres.py` holds `PostgresStore`, the `TransactionalStore` whose primitives are SQL (§5), together with its administration surface (§7) and the database and role names the deployment reads (§11.3).
- `bro/trails/server/schema/` holds the schema revisions (§9).
- `bro/trails/server/operations.py` keeps the backend-neutral recompute and check computations;
  everything it writes to DynamoDB or S3 goes.
- `NetworkStore` and trails-server keep their roles;
  §10 lists what changes in them.

## 4. The shared contract implementation

### 4.1 Trail transactions

Every write to an existing trail runs inside a trail transaction on that trail.
The transaction reads the trail's stored header when it opens, and the contract operation decides everything from what the transaction shows it;
two writers on one trail therefore serialize, and no writer acts on a header another writer changed after the read.

A `PostgresStore` trail transaction is one database transaction that keeps the trail's header row locked (`SELECT … FOR UPDATE`) from its open to its commit;
an exception rolls it back, leaving the trail as it was.
A `LocalStore` trail transaction is the trail's exclusive `flock`, its writes replacing files atomically one at a time;
§6.3 states what `LocalStore` guarantees across a crash.

### 4.2 Storage primitives

| Primitive | Effect | Trail absent |
|---|---|---|
| `writing(trail_id)` | opens a trail transaction, yielding the handle below | `TrailNotFound` |
| `create(header, rows)` | stores a new trail with its initial rows, all or nothing; answers `False` and stores nothing when the id is taken | — |
| `read_header(trail_id)` | the stored header document | `TrailNotFound` |
| `read_rows(trail_id, start, count)` | the stored row documents at ordinals `start` through `start + count - 1`, fewer past the extent | `TrailNotFound` |
| `list_headers(selectors, since, until, after, count)` | up to `count` stored header documents matching every selector given, newest first by `(started_at, id)`, each ordered strictly after the key `after` when it is given | — |
| `segment_headers(segments)` | the stored header documents whose upgraded `native.segment` is one of `segments` | — |
| `holds_record(trail_ids, uuid)` | whether any of the trails stores a row whose upgraded `uuid` is `uuid` | — |
| `tool_payloads(digests)` | the stored canonical payload of each digest the store holds | — |

The selectors are the contract's `harness`, `bro` and `forked_from`;
`since` and `until` bound `started_at`, both inclusive.

The trail transaction's handle:

| Member | Effect |
|---|---|
| `header` | the stored header document read at open |
| `rows(start, end)` | the stored row documents at ordinals `start` through `end - 1` |
| `digests(start, end)` | the `payload_sha256` of each of those rows, without reading their bodies |
| `tool_payloads(digests)` | as the store's primitive, read inside the transaction |
| `append(rows)` | stores the rows at the ordinals following the stored ones |
| `replace_rows(rows)` | makes `rows` the trail's whole row stream |
| `write_header(document)` | replaces the stored header document |
| `put_tools(payloads)` | stores each tool blob not yet stored, keyed by its digest |
| `delete()` | removes the trail and its rows; tool blobs stay, since trails share them |

Inside a trail transaction the shared implementation reads only through the handle, so a `PostgresStore` operation never holds one pooled connection while it waits for another.
A backend interprets a document only to compute its derived columns;
validation, folding and every contract decision belong to `TransactionalStore`.

### 4.3 Operations

Each operation keeps the contract the repository defines for it (`bro/trails/store.py`, and the "Surfaces" section of `bro/trails/AGENTS.md`).
This section states how each maps onto the primitives and what changes.
Times are the server's clock as `%Y-%m-%dT%H:%M:%S.%fZ` UTC strings, as today.

- `blaze(request)`:
  validates the request and opens its body through the trail format.
  With lineage evidence it asks the format's resolver, which reads through `find_segment_trails` and `holds_record` outside any transaction;
  a declined adoption answers `{adopted: false, reason}`, and an attach verdict continues as the attach below.
  Otherwise it builds the header
  — a fresh lulid `id`, `started_at` and `last_alive_at` set to now, `extent` 0, the native fields a fork inherits, and the cuts a verdict minted —
  folds the opened records into rows, and calls `create`.
  `create` answering `False` for a fresh lulid is an impossible state, and raises.
  The number of opening records has no limit of its own.
- The attach a lineage verdict asks for:
  in a trail transaction on the verdict's trail, migrates the trail (below).
  It answers `{adopted: false, reason: backends.ATTACH_CONTENDED}` unless the extent equals the one the verdict verified;
  otherwise it restamps the header (`backends.attached_header`) with `last_alive_at` set to now.
- `append_records(trail_id, offset, records, tools)`:
  in a trail transaction, migrates the trail, then compares `offset` with the extent.
  On a mismatch it answers `{extent, appended: 0, duplicate: true}` when the extent equals `offset + len(records)` and `digests(offset, extent)` equals the records' payload digests, and raises `AppendConflict(offset, extent)` otherwise.
  On a match it verifies and stores the carried tool blobs, folds the records into rows starting from the stored header, appends them, and writes the header with the new fold and `last_alive_at` set to now.
- `set_subject`, `end_trail` and `keepalive`:
  each is one trail transaction that changes the header as today and writes it.
- `migrate_trail(trail_id)`, and the migration an append, an attach and a recompute perform first:
  when the stored header's format is older than the current one, upgrades every row and the header and writes them in the same transaction (`replace_rows`, `write_header`), reporting how many rows were older;
  a current header changes nothing.
  The other writes keep the header in its stored format, as today.
- `delete_trail(trail_id)`:
  raises `TrailHasForks` while any trail's `forked_from` names the trail, then removes it in a trail transaction and answers `{trail_id, extent}`.
  Nothing records what was removed.
- `begin_import(header)`:
  builds the unsealed header (`importing.imported_header`), requires the parents it points at (`importing.require_parents`), and calls `create` with no rows.
  A `LocalStore` built with `external_parents_ok` accepts absent parents, as today.
  `True` answers `{trail_id, extent: 0, created: true}`;
  `False` reads the stored header, applies `importing.verify_same_import`, and answers `{trail_id, extent, created: false}`.
- `import_rows(trail_id, offset, rows, tools)`:
  in a trail transaction, validates the rows (`importing.validate_rows`) and raises `AppendConflict` for an `offset` past the extent.
  It verifies the rows already stored through `digests` (`importing.verify_same_rows`) and checks the room the import mark leaves;
  it stores the carried tool blobs and refuses rows naming a blob that is neither carried nor stored.
  It then appends the remainder verbatim and writes the header's new extent.
- `seal_import(trail_id)`:
  in a trail transaction, answers `{trail_id, extent, duplicate: true}` for a sealed trail and refuses until every recorded row is stored;
  it then writes the header with the fold `importing.sealed_fields` computes from every row, the recorded `end`, and no import mark.
- `get_trail`, `list_trails` and `find_segment_trails`:
  serve upgraded, projected headers (§6.1).
  A listing page holds up to `limit` headers, 1 to 100, and is read as `limit + 1`;
  its `next` is an opaque encoding of the last served header's `(started_at, id)`, null exactly when no header follows.
- `get_step`, `get_steps` and `get_messages`:
  check the header's format, read a page of `limit` rows (1 to 500) as `limit + 1`, and answer `next` and `through` as today.
- `get_tool(sha256)`:
  refuses a malformed digest, raises `ToolNotFound` when no payload is stored, and refuses a stored payload whose digest differs.
- `holds_record`:
  the primitive, answered as it is.

A tool blob an append or an import carries must be keyed by the SHA-256 of its canonical JSON (`model.canonical_json_bytes`);
a stored payload is verified against its digest whenever it is read.

## 5. PostgreSQL representation

### 5.1 Tables

Schema revision 1 creates:

```sql
CREATE TABLE trails (
  id             text COLLATE "C" PRIMARY KEY,
  document       json NOT NULL,
  started_at     text COLLATE "C" NOT NULL,
  harness        text NOT NULL,
  bro            text,
  forked_from_id text COLLATE "C",
  segment        text
);
CREATE INDEX trails_newest ON trails (started_at DESC, id DESC);
CREATE INDEX trails_by_harness ON trails (harness, started_at DESC, id DESC);
CREATE INDEX trails_by_bro ON trails (bro, started_at DESC, id DESC) WHERE bro IS NOT NULL;
CREATE INDEX trails_by_fork_parent ON trails (forked_from_id, started_at DESC, id DESC)
  WHERE forked_from_id IS NOT NULL;
CREATE INDEX trails_by_segment ON trails (segment) WHERE segment IS NOT NULL;

CREATE TABLE steps (
  trail_id       text COLLATE "C" NOT NULL REFERENCES trails (id) ON DELETE CASCADE,
  step_id        integer NOT NULL CHECK (step_id >= 0),
  document       json NOT NULL,
  payload_sha256 text NOT NULL,
  uuid           text,
  PRIMARY KEY (trail_id, step_id)
);
CREATE INDEX steps_by_uuid ON steps (uuid) WHERE uuid IS NOT NULL;

CREATE TABLE tools (
  sha256  text COLLATE "C" PRIMARY KEY,
  payload text NOT NULL
);
```

The store keeps its own bookkeeping in `schema_revisions` (§9).

### 5.2 Rules

- `document` holds the document as JSON text, encoded as `json.dumps(document, ensure_ascii=False, separators=(',', ':'), allow_nan=False)`, the encoding `LocalStore` writes.
  The `json` type keeps that text exactly:
  object key order survives, and a string carrying `\u0000`
  — a tool output with a NUL byte —
  is stored, where `jsonb` would refuse it.
  A document whose text cannot be encoded as UTF-8, such as one with an unpaired surrogate, is refused as `InvalidRequest`.
- Every write of a header document writes its derived columns, computed from `formats.upgrade_header(document)`:
  `started_at`, `harness`, `bro`, `forked_from_id` from `forked_from.trail_id`, and `segment` from `native.segment`, each null where the header has none.
  Every write of a row document writes `payload_sha256`, and `uuid` when the row carries a string `uuid`, computed from `formats.upgrade_row(document)`.
  Deriving from the upgraded document keeps the columns right for a trail stored in an older format, such as one just imported as recorded.
- Only keys, indexes and filters read derived columns;
  a served header or row is built from `document` alone.
- `COLLATE "C"` compares the text keys bytewise, the order in which the other backends compare strings, so `since`, `until` and the listing order agree with theirs.
- `tools.payload` is the canonical JSON text whose SHA-256 is `sha256`.
- A trail's `steps` rows go with it (`ON DELETE CASCADE`);
  tool blobs are shared, and nothing deletes them.

### 5.3 Example

A native trail with two rows, a Claude trail, and a fork of that Claude trail:

| `trails.id` | `started_at` | `harness` | `bro` | `forked_from_id` | `segment` |
|---|---|---|---|---|---|
| `01aa…` | `2026-11-02T10:00:00.000000Z` | `bro` | `dev` | null | null |
| `01bb…` | `2026-11-02T11:00:00.000000Z` | `claude` | null | null | `5f2c…` |
| `01cc…` | `2026-11-02T12:00:00.000000Z` | `claude` | null | `01bb…` | `7a90…` |

| `steps.trail_id` | `step_id` | `payload_sha256` | `uuid` |
|---|---|---|---|
| `01aa…` | 0 | `9e1d…` | null |
| `01aa…` | 1 | `47b0…` | null |

The first header's `document` is its whole JSON, `{"id":"01aa…","format":1,"harness":"bro",…,"extent":2,"native":{…}}`.
`list_trails(bro='dev')` reads `trails_by_bro`, and serves each `document` upgraded and projected;
`delete_trail('01bb…')` raises `TrailHasForks` naming `01cc…`, found through `trails_by_fork_parent`.
If a later trail format moved `native.segment` to `native.claude.segment`, a Claude header still stored in format 1 would keep its `segment` column filled, because the column is computed from the header after its upgrade.

## 6. Behaviour shared by both backends

### 6.1 Served headers and unreported trails

A served header is the upgraded header document plus `usage`, the document's `native.usage`, and `models`, that object's keys in sorted order.
When the stored `end` is null and `last_alive_at` lies an hour or more in the past, the served header's `end` is `{at: last_alive_at, inference: "unreported"}`;
nothing is written.
A writer that comes back advances `last_alive_at` with its next append or keepalive, so the trail reads as open again until the writer ends it.
One function computes the projection for every backend, and the hour is its constant.

For example, a trail last alive at 10:00 with a null `end` reads as open at 10:59, and as ended with `{at: "2026-11-02T10:00:00.000000Z", inference: "unreported"}` at 11:00;
a keepalive at 11:05 makes it read as open again.

An export carries the served header, so a trail exported while it reads as unreported is imported with that `end` stored, which is a valid recorded end (`model.validate_recorded_end`).

### 6.2 What changes for LocalStore

`LocalStore` becomes the file-backed `TransactionalStore`.
Its layout and every value it serves stay as they are, except that:

- a delete removes the trail's directory and writes nothing under `<root>/manifests/`, answering `{trail_id, extent}`;
- a blaze builds its trail under `<root>/staging/` and renames it into place, as an import's begin already does, since both go through the shared `create`.

Its layout reads for transfers (`stored_trail_ids`, `stored_header`, `stored_rows`) and its `external_parents_ok` flag stay its own.

### 6.3 LocalStore across a crash

A `LocalStore` write changes the row file before the header, so a crash between the two can leave rows past the header's extent, rows no caller was ever answered for.
Opening a trail transaction truncates the row file to the header's extent, and the caller's retry writes those rows again.
A row file shorter than the header's extent is a corrupt trail, refused with `ValueError`.
Without the truncation, an append after such a crash would write past the orphaned rows and misalign every later ordinal.

## 7. PostgreSQL administration

`PostgresStore` alone serves the repairs, as `DynamoStore` did:
trails-server mounts their routes when the hosted store is a `PostgresStore` and answers 501 otherwise.

- `recompute(trail_id)`:
  in one trail transaction, migrates the trail, refolds every row from the minted state (`rows.replay`), rewrites each row's kind, attributes and usage contribution with `replace_rows`, and writes the header's fold;
  it answers `{trail_id, extent, turn_count, usage}`.
- `check(trail_id=None)`:
  for one trail, compares its stored fold and rows with a refold over one read snapshot and answers the differences as today;
  without a trail, it checks every trail and adds the record uuids more than one trail stores, found by one `GROUP BY` over `steps.uuid`.

The refold and the comparison stay in `bro/trails/server/operations.py`, which writes nothing itself.
`relink` is gone from the store, the server and `NetworkStore`.

## 8. Concurrency and failure

- Transactions run at PostgreSQL's default `READ COMMITTED` isolation.
  The header row lock of §4.1 is what serializes the writers of one trail;
  writers on different trails never wait for each other.
- A read operation sees one state of a trail:
  `PostgresStore` reads in one read-only transaction at `REPEATABLE READ`, and `LocalStore` under the trail's shared lock.
- Retries stay safe through the contract itself:
  an append whose commit landed but whose answer was lost is answered as a duplicate when retried, and an import's begin, rows and seal each answer a repeat as themselves.
  `NetworkStore` still never retries a blaze, which could mint a second trail.
- A lost connection
  — a failover, a database restart, a dropped network path —
  and a database refusing work for a transient reason raise `TransientUnavailable`, which trails-server answers with 503.
  The transient reasons, by SQLSTATE, are:
  - class `08`, the connection exceptions;
  - `57P01` through `57P03`, a shutdown or a database not yet accepting connections;
  - `25006`, a read-only transaction, which a writer demoted by a failover answers;
  - `40001` and `40P01`, a serialization failure and a deadlock;
  - `55P03` and `57014`, a lock timeout and a statement timeout.

  The connection the failure happened on is discarded, never lent again.
- trails-server retries nothing itself;
  each client keeps its own schedule, `NetworkStore` retrying appends, ends and import requests three times over about 2.6 seconds.
- Every connection runs with a 20-second statement timeout and a 10-second lock timeout, so a request waiting on the database is answered within the client's 30-second socket timeout.
- `/health` touches no database, so a database failover never fails the load balancer's health check or replaces a task.

For example:

- Two recorders append at offset 7 to a trail whose extent is 7.
  The first takes the header lock and commits extent 9;
  the second then reads extent 9, finds that its records' digests differ from rows 7 and 8, and raises `AppendConflict(7, 9)`.
- An append at offset 7 commits, but the connection drops before the answer leaves.
  trails-server answers 503, `NetworkStore` retries, and the retry reads extent 9 with matching digests and answers `{extent: 9, appended: 0, duplicate: true}`.
- An import whose client died is run again:
  the begin answers `created: false` with the stored extent, the rows already stored are re-sent and verified by digest, the rest are appended, and the seal finishes it.
- During a failover, a native one-shot run's append fails every retry, so its turn fails and the run ends `error`;
  [#1189](https://github.com/dzhioev/bro/issues/1189) addresses that.
  A Claude recorder re-sends the same lines on its next tick, once the new writer answers.

## 9. Schema revisions

- A schema revision is a file `bro/trails/server/schema/NNNN_<name>.sql`, whose four-digit number is its revision;
  revision 1 is §5.1.
- Opening a `PostgresStore` applies the pending revisions in one transaction, which first takes a transaction-scoped advisory lock so that two servers starting together apply each revision once.
  The transaction creates `schema_revisions (revision integer PRIMARY KEY, applied_at timestamptz NOT NULL)` where it is absent, runs every file numbered above the newest recorded revision in order, and records each.
  It runs without the statement timeout of §8, since a revision may rewrite a populated table.
- A database recording a revision newer than the newest file the store ships is refused at open, naming both revisions, so no server runs against a schema it does not know.
- A revision keeps the previous release's server working, since a rolling deploy runs both against it for a while:
  a change adds first, and removes in a later release.
  Rolling back across a revision the older server lacks is refused at its startup;
  that is a known limit.
- The connecting role must be able to create tables in the database's `public` schema;
  on the hosted deployment §11.3 grants it that.

## 10. Server, clients and tools

This section describes the end state;
§13 orders the changes, and until its step 7 the DynamoDB pieces it removes keep serving.

### 10.1 trails-server

- The server opens its hosted store before it listens, so a `PostgresStore` it cannot reach, or whose schema it cannot bring current, fails the start.
- `TransientUnavailable` from any store call answers 503, which clients treat as transient, like any status of 500 or above.
- The `recompute` and `check` routes serve a `PostgresStore` (§7);
  the relink route is removed.
- The delete route answers `{trail_id, extent}`, naming no manifest (§4.3).
- An append or an import no longer refuses a body over 10 MiB:
  `BodyTooLarge` and its 413 answer go, and the 64 MiB request cap still answers 413.
- The unreported-trail sweep goes with `DynamoStore` (§6.1).
- Store calls stay off the event loop, and the store's connection pool holds as many connections as the server runs store calls at once, so no call waits for a connection.

### 10.2 The `trails` credential for PostgreSQL

A trails-server's `trails` credential selects `PostgresStore`, logging in with a password:

```json
{
  "backend": "postgres",
  "connection": {"host": "db.example", "dbname": "trails", "user": "trails", "password": "…", "sslmode": "verify-full"}
}
```

or with an RDS IAM token:

```json
{
  "backend": "postgres",
  "connection": {"host": "trails.cluster-….rds.amazonaws.com", "port": 5432, "dbname": "trails", "user": "trails_server", "sslmode": "verify-full"},
  "iam_region": "us-east-1"
}
```

- `connection` holds libpq connection keywords, passed to libpq as they are;
  `host`, `dbname` and `user` are required.
  libpq refuses an unknown keyword at the first connection, which the server makes before it listens.
- `iam_region` is optional.
  When present, the store mints an RDS IAM authentication token for every new connection, signed for that region with the process's AWS credentials, and logs in with it;
  `connection` must then carry `sslmode` `verify-full` and no `password`.
  `boto3` is imported only in this mode, and comes from the `aws` extra.
- Any other top-level field is refused.
- A connection's TLS follows libpq:
  `sslmode`, `sslrootcert`, or the `PGSSLROOTCERT` environment variable.
- The pool validates an idle connection before lending it and replaces connections past a maximum lifetime.
  A minted token is valid for 15 minutes but checked only at login, so an open connection outlives it.
  `close()` closes the pool.
- `build_store` loads `bro.trails.server.postgres` lazily for `backend` `postgres`;
  its known backends are `local`, `postgres` and `service`.
- The `trails-server` extra carries `aiohttp`, `psycopg[binary]` and `psycopg-pool`;
  `boto3` leaves it.

### 10.3 NetworkStore and the contract surface

- `NetworkStore.relink` goes.
- The dead spill path goes:
  `model.SpillDescriptor` and `spill_descriptor`, `NetworkStore.fetch_spilled_body` and `resolve_body`, `TrailsStore.resolve_body` and its callers' use of it, and the recorded display's spilled body.
  trails-server has served every body inline since it became a proxy over the store, so nothing produces a descriptor.
- `UnsupportedOperation` stays, for the 501 the repairs answer on a `LocalStore` backend.

### 10.4 `trails export` and `trails import`

- `trails export --all -o DIR` exports every trail the store lists, with no ancestry walk;
  `--all` and named trail ids exclude each other.
- Each exported trail is the snapshot its header names:
  exactly its first `extent` rows, read with `collect_steps`, so a trail still being written exports consistently instead of failing the import's row count.
- `--jobs N`, on both commands and 1 by default, copies trails in waves by lineage depth
  — a trail's depth is one more than its deepest parent within the copy —
  and runs each wave's trails on `N` workers;
  every parent still precedes its children.
- `trails delete` prints each removed trail and its extent.

## 11. The hosted deployment

### 11.1 Cluster

`TrailsServerStack` gains the cluster:

- Aurora PostgreSQL, major version 17, its minor version following AWS's automatic minor upgrades.
- A Serverless v2 writer in one of the two availability zones the platform VPC spans, and a Serverless v2 reader in the other, which scales with the writer;
  each runs at 0.5 to 4 ACU (Aurora capacity units).
  The reader takes no traffic:
  it is the standby a failover promotes.
- The platform VPC's two public subnets, the only subnets `PlatformStack` declares.
  The instances are not publicly accessible
  — stated explicitly, because the instance construct makes an instance in a public subnet public by default —
  and the database security group admits port 5432 only from the trails service's security group.
- IAM database authentication on, the RDS Data API on, storage encrypted, deletion protection on, the cluster retained when it leaves the stack, and `rds.force_ssl` set to 1.
- A generated master credential in Secrets Manager, which only the role bootstrap uses (§11.3).
- The default database `trails`.

### 11.2 Backups

- Automated backups are retained for 7 days, allowing a restore to any point within them.
- An AWS Backup plan selects the cluster and keeps its snapshots in a retained vault:
  a weekly snapshot on Sundays at 03:00 UTC, deleted after 90 days, and a monthly snapshot on the 1st at 03:00 UTC, deleted after 365 days.
- A restore creates a new cluster from a snapshot by hand;
  this design automates no restore.

### 11.3 Role bootstrap

- trails-server logs in as the role `trails_server`;
  the database and role names are constants of `bro.trails.server.postgres`, which the stack reads.
- A custom resource runs at every create and update of the stack, after the cluster's instances exist.
  Through the RDS Data API and with the master credential, it executes one idempotent statement:
  it creates the login role `trails_server` where absent, grants it `rds_iam`, and grants it `CREATE` on the `trails` database and on its `public` schema, so the role owns every object it creates.
  The master never becomes a member of `trails_server`:
  through it the master would hold `rds_iam`, which makes a role's login IAM-only and would lock the Data API out.
- The task role is granted `rds-db:connect` as `trails_server`.
- The Data API stays enabled;
  nothing in the service uses it.

### 11.4 Store configuration and image

- The switch revision writes the store-config parameter in §10.2's shape:
  the writer endpoint and its port, `dbname` `trails`, `user` `trails_server`, `sslmode` `verify-full`, and the stack's region as `iam_region`.
  All traffic goes to the writer endpoint.
- The image installs `bro[trails-server,aws]`;
  the `aws` extra brings `boto3`, which the image's SSM credential source and the IAM login use.
- The image carries Amazon RDS's global CA bundle at a pinned digest and points `PGSSLROOTCERT` at it.

## 12. Moving the hosted data

### 12.1 Preconditions

- The hosted store holds no unsealed import, and every `forked_from` and `summoned_by` names a stored trail;
  both held when the store was audited for this design.
- The trail `01kydxed84-cbxcev16-r3smp6xv` has been deleted (§13, step 4):
  its `forked_from` names step 303 of a parent with 111 rows, and no other stored fork points past its parent's rows.
- The host running the copy has room for the export, several gigabytes in the local store layout.
- The session running the copy holds a `trails` credential whose token reads, writes and administers, and records its own run locally (§12.3).

### 12.2 Rehearsal

In a short quiesced period before the window:

1. Deploy the switch revision and restart the service, so its tasks open PostgreSQL and apply schema revision 1.
2. Through the service, record a smoke trail, read it back, and delete it.
3. Deploy the previous revision and restart the service, back on DynamoDB.

The rehearsal proves the network path, the IAM login, TLS, the schema and the pool before the window depends on them.

### 12.3 The window

1. Quiesce:
   no session records through the service.
   The check is that no served header has a null `end` with `last_alive_at` inside the last three minutes, since a live recorder refreshes it at least every minute.
   The sessions that run the window record locally, launched without the `trails` credential or with `TRAILS_DISABLED`.
2. Run `trails export --all --jobs 8 -o DIR` against the DynamoDB-backed service.
3. Deploy the switch revision, restart the service, and wait until every task runs it and the previous tasks have drained.
4. Run `trails import --jobs 8 DIR` against the PostgreSQL-backed service.
5. Verify:
   the per-trail extents the import prints equal the ones the export printed, the service lists the same number of trails, and `rewind show` renders a sample including a Claude fork chain and the largest trail.
6. Let sessions record again.

At the store's present size, about 6k trails and 1M rows, the window lasts on the order of an hour;
the import's share depends on the upload bandwidth of the host running it.

A session that writes through the service during the window loses its recording from its first failed write.
A native one-shot run's turn fails ([#1189](https://github.com/dzhioev/bro/issues/1189)).
A Claude recorder keeps failing while its own extent is ahead of the stored one, until a restart re-attaches it and its lineage attach takes every line the store lacks.

### 12.4 Rollback

Until `DynamoStore` is deleted, the move reverses with the same tools, in a window quiesced like §12.3:
export `--all` from the PostgreSQL-backed service, deploy the previous revision, restart, and import into the DynamoDB-backed service.
A trail that grew after the window collides there with its older copy, so it is deleted from DynamoDB before that import.

## 13. Rollout

1. Code:
   `TransactionalStore` and both backends on it, `PostgresStore` with its schema and administration, the server and tool changes of §10, the image changes of §11.4, and the tests of §14.
   `DynamoStore` still serves the hosted store, with its sweep and its relink;
   trails-server mounts the administration routes for whichever of the two it hosts.
2. Deploy:
   the stack adds the cluster, its security group, the role bootstrap, the backup plan and the `rds-db:connect` grant.
   The service stays on DynamoDB, running the image that carries both backends.
3. The rehearsal (§12.2).
4. Delete the legacy fork through the DynamoDB-backed service, with `trails delete`.
5. The window (§12.3).
6. Verification:
   7 days on PostgreSQL, with the rollback of §12.4 available.
7. Code:
   the deletions of §15.
8. Deploy:
   the stack stops owning the DynamoDB tables and the spill bucket, which stay in the account, retained;
   the task role loses their grants, and `spillover_bucket_name` leaves the deployment configuration.
9. With the owner's go-ahead, delete the retained tables and the bucket, the records under its `trails/migrations/` included.
   The export of step 5 stays as the archive of the DynamoDB-era data.

Steps 2, 3, 5, 8 and 9 change the live account, with the `aws` and `infra` credentials;
steps 3, 4 and 5 also read and write through the service, with the administering `trails` token.

## 14. Testing

- A test helper starts one PostgreSQL 17 per test worker from the `postgresql-binaries` wheel
  — `initdb` and `pg_ctl` in a temporary directory, a Unix socket, trust authentication —
  and builds a template database at the newest schema revision.
  Each test gets a fresh database created from the template, and a context manager stops the server.
  It runs in the unit stage, in sessions and in CI alike, without Docker.
- The contract suite runs on four stores:
  `LocalStore`, `PostgresStore`, and a `NetworkStore` over a loopback server backed by each of them.
  Its format tests reach stored documents through an accessor per backend instead of the local layout's files.
- PostgreSQL tests cover:
  - two concurrent appenders at one offset, attach contention, and two begins of one import;
  - derived columns computed from upgraded documents, and the bytewise listing order;
  - a document carrying `\u0000` surviving a round trip with its key order;
  - two stores opening an empty database together, and the refusal of a newer schema revision;
  - a terminated backend raising `TransientUnavailable`, and the next call succeeding;
  - one token minted per new connection in IAM mode, with `boto3` stubbed;
  - the credential's validation.
- `LocalStore` tests cover the crash rule of §6.3.
- Server tests cover the 503 answer, the 501 repairs on a `LocalStore` backend, and the absent relink route;
  transfer tests cover `--all`, the extent-bounded snapshot, and the waves of `--jobs`.
- CDK tests assert the cluster's properties, the security group rule, the grant, the custom resource, the backup plan, and the switch revision's store configuration.
- The image check adds a run against a PostgreSQL 17 container with a password credential, recording and projecting through the image as the local run does.
- `postgresql-binaries` joins the development dependencies;
  `moto` and `server/dynamo_test.py` leave.

## 15. Deletions

In step 7 of §13:

- `bro/trails/server/dynamo.py`, `dynamo_types.py`, `dynamodb_reserved_words.txt` and `dynamo_test.py`;
- the DynamoDB and S3 writes, the delete and the relink in `operations.py`;
- the server's sweep, relink route, `BodyTooLarge` handling and `DynamoStore` references;
- the `dynamo` branch of `build_store`;
- `NetworkStore.relink` and the spill path of §10.3;
- `store.delete_manifest` and `store.manifest_name`;
- `moto`;
- the stack's tables, bucket and their grants, and `TrailsConfig.spillover_bucket_name` with its default.

`LocalStore`'s manifests already go in step 1 (§6.2).

## 16. Documentation

- `bro/trails/AGENTS.md`:
  the architecture, `TransactionalStore`, `PostgresStore`, trails-server, the surfaces (a delete keeps no manifest, relink is gone, the 503), and the tests.
- `harnesses/claude/trails/AGENTS.md`:
  the lineage index answered by the segment and uuid indexes of §5.1 in place of DynamoDB's.
- `bro/setup/AGENTS.md`:
  the `trails` credential's PostgreSQL shape in place of the DynamoDB one.
- `oops/AGENTS.md`, `oops/README.md` and `oops/cdk/README.md`:
  the cluster, the backups, the role bootstrap, and the image's extras.

## 17. Decision record

Every decision below was settled by the owner on 2026-11-03.

| # | Decision | Applied in |
|---|---|---|
| 1 | `LocalStore` and `PostgresStore` share one contract implementation over storage primitives, rather than `PostgresStore` standing alone | §4, §6 |
| 2 | Documents are exact JSON text (`json`), with derived columns for keys and indexes, rather than `jsonb` | §5 |
| 3 | An unreported trail is inferred when its header is read, and no sweep stamps it | §6.1 |
| 4 | Deleting a trail keeps no record of what it removed, on every backend | §4.3, §6.2 |
| 5 | `relink` is dropped rather than ported | §7 |
| 6 | A lost database connection answers 503 and trails-server retries nothing; native recorder resilience is [#1189](https://github.com/dzhioev/bro/issues/1189) | §8 |
| 7 | The hosted role is created at deploy time through the RDS Data API, which stays enabled | §11.3 |
| 8 | The hosted data moves in one quiesced window after a rehearsal, with `trails export --all` and extent-bounded snapshots | §10.4, §12 |
| 9 | `trails export` and `trails import` copy in parallel with `--jobs` | §10.4 |
| 10 | Aurora PostgreSQL 17 at 0.5 to 4 ACU per instance, with deletion protection | §11.1 |
| 11 | Backups: 7 days of automated backups, weekly snapshots kept 90 days, monthly snapshots kept 365 days | §11.2 |
| 12 | Tests run against PostgreSQL from the `postgresql-binaries` wheel | §14 |
| 13 | `DynamoStore` is deleted after a 7-day verification period, and DynamoDB support is not kept as a consumer option | §13, §15 |
| 14 | `PostgresStore` works with any PostgreSQL, IAM login is an optional mode, and `boto3` leaves the `trails-server` extra | §10.2, §11.4 |
| 15 | The legacy fork `01kydxed84-cbxcev16-r3smp6xv` is deleted before the move | §12.1, §13 |

## 18. Assumptions and evidence

Verified against the hosted account while designing:

- The platform VPC has exactly two subnets, both public, in two availability zones, and no NAT gateway, as `PlatformStack` declares.
- The hosted store holds no unsealed import and no trail pointing at a missing parent.
  Every stored header and row is in format 1, and the largest trail holds a few thousand rows.
  The legacy fork is the only fork whose bound lies past its parent's rows.

From AWS's and PostgreSQL's documentation:

- Aurora PostgreSQL 17 runs on Serverless v2, and supports the RDS Data API from 17.4.
- An RDS IAM authentication token lasts 15 minutes, is checked only at login, and requires TLS.
- A role holding `rds_iam` logs in only with IAM tokens, so the master credential the Data API uses never receives it.
- The `json` type keeps its input text exactly, `\u0000` escapes included;
  `jsonb` refuses `\u0000` and reorders object keys.

Unverified:

- The window's length, which rests on #1166's measured read of the whole store (about 18 minutes of server time, sequentially) and on the copying host's upload bandwidth.
- `postgresql-binaries` running in session containers and on CI runners, which the first stage adding it proves.
- Whether RDS treats `rds_iam` held through another role as held;
  §11.3 keeps the master out of `trails_server` either way.
