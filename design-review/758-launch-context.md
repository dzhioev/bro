# #758 design review

A design under review, never to be merged:
the pull request carrying this file closes unmerged once the design is settled, and the design itself lives on https://github.com/dzhioev/bro/issues/758 under `## Design`.
Its `## Goal`, `## Requirement` and `## Research` sections are the context it answers.

## Design

The git state moves into the header as a `git` object, and every stored launch context is folded into its trail's header once, so the separate launch-context channel can go.
Three landings on `master` carry it, with a trails-server deploy after the first and the last:
an expand landing that accepts both shapes, a switch landing that moves writers and readers, and a contract landing that deletes everything transitional.
A stage in the first landing makes claude sessions get the root `AGENTS.md` without depending on Claude Code's remote feature flags.

### Header vocabulary

Both fields are additive, so the trail format stays 1.

- `git: {repo, url, branch, base_sha}` — the git state of the session's attached tree, a top-level header field beside `location`.
  - `repo` is the attachment identity ride publishes as `RIDE_REPO`: the checkout path, or the normalized URL of a URL attachment.
  - `url` is the session tree's `origin` when it names a network remote, normalized by `bro.base.git_url.normalize_git_url`.
    A local path or `file://` origin leaves it out.
    It is no rewrite of its own:
    ride's clone step already turns a GitHub scp origin into its https form when it sets the tree's `origin` (`ride/ride/workspace/clones.py`).
  - Credentials are stripped from both: the userinfo of a `scheme://` URL is dropped (the scp form's `git@` names a user and carries no secret).
  - `branch` and `base_sha` are `RIDE_BRANCH` / `RIDE_BASE_SHA`.
  - Each key is an optional non-empty string, an unknown key is refused, and an empty object is refused:
    a trail without git state has no `git` at all
    — a detached session, a run outside a managed session.
  - A blaze writes it and an attach restamps it latest-wins beside `location`, `version` and `hold` (`backends.attached_header`), so it names the latest launch's git state.
    The stored record it replaces always named the minting launch's.
  - A trail recorded before the change gets `branch` / `base_sha` from its stored record and no `repo` or `url`, which were never recorded.
- `launch_context: [...]` — history only:
  the records a trail's stored launch context held besides its git state, verbatim and in stored order
  (the appended system prompt, the MCP servers line, the root instructions file).
  No writer sends it and no blaze accepts it.
  It exists on trails recorded before the change, and on trails that recorders older than #756 open during the transition (ppp's and kap's pins, bundles frozen before #756).
  A stored git record carrying fields beyond `branch` / `base_sha` (the cw era's `base_ref`) stays in it whole as well, besides feeding `git`.
- `rewind show` lists `repo`, `url`, `branch` and `base sha` among the header fields beside `workspace` and `host`,
  and renders `launch_context` as one more header field through the display's YAML rendering, long texts as literal blocks, which `rewind grep` searches like any rendered line.
  `LaunchContextEntry`, `RecordKind.LAUNCH_CONTEXT` and the `SESSION CONTEXT` block go.

### The fold

The fold turns a stored launch context into header fields; it is transitional and leaves with the contract landing.

- The context must be a list of records, each a `{kind, subtype, title}` object with `content` or `fields`, and at most one of them `git/state`; anything else is refused, naming the trail.
  The service holds 1,169 stored contexts, every one a list with at most one git record (`## Research`, "Fold rule inputs").
- The git record's `branch` / `base_sha` become `git` when the header has no `git`.
  A header already carrying `git` keeps it, since an attach after the mint restamped it with a later launch's state.
- The other records become `launch_context` when the header has none, and must equal it when it has one;
  a difference is refused, naming the trail.
- Folding the same context twice leaves the header as the first fold left it, and folding the context the route rebuilds from a header (below) into that header changes nothing.

### Writers

- `ManagedSession` exposes `git` in place of `git_record`, read off `RIDE_REPO`, `RIDE_REPO_URL`, `RIDE_BRANCH` and `RIDE_BASE_SHA`.
  Ride publishes `RIDE_REPO`, `RIDE_BRANCH` and `RIDE_BASE_SHA` together for an attached tree (`ride/ride/session.py`), and one of them without the others is refused, as a lone `RIDE_BRANCH` or `RIDE_BASE_SHA` is today.
- The launcher publishes `RIDE_REPO_URL` beside `RIDE_REPO` wherever that is set (the launch and runner environments, the container `-e`, `do-ride`),
  read from the session tree's `origin` on the host side,
  and `bro/reference/ride.md`'s "Forwarded env vars" documents it.
  Launcher and recorder come from one runtime bundle, so they switch together.
- Both recorders send `git` as a new top-level `BlazeRequest` field and stop sending `body.launch_context`:
  the claude recorder on every blaze, the bro recorder when it starts a trail.

### Stores and server, per landing

Landing 1 (expand) accepts both shapes:

- `BlazeRequest` and `model` validate `git`; `from_wire` / `to_wire` carry it.
- A blaze carrying `body.launch_context` (an old writer) is folded:
  at a mint into the new header, with no context object or file written;
  at an attach, its git record becomes the request's `git` before the restamp, as the attach writes no launch records.
- `begin_import` folds a carried `launch_context` into the imported header and stores no context;
  `verify_same_import` compares the folded forms of the stored and the carried trail.
- `GET /v1/trails/{id}/context` answers old readers from the header:
  a `git/state` record built from `git`, followed by `launch_context`;
  a Dynamo trail still holding a pointer is folded first, so the answer is the same before and after the backfill.
- `LocalStore` stops writing and reading `context.json`:
  local stores are not kept, so an old local trail loses its stored context, and a layout imported through `transfer.LayoutSource` arrives without one.
- `DynamoStore` gains two transitional administration operations,
  forwarded like `recompute` / `check` / `relink` (concrete `NetworkStore` methods and `/v1/admin/*` routes, answering 501 on other backends),
  and the `trails` CLI drives them as `trails fold-contexts [--dry-run] [--drop] (--all | TRAIL_ID…)`:
  - fold: reads the object a trail's `context_s3` or legacy `native.context_s3` names, folds it, and writes `git` / `launch_context` with an update conditional on the fields it read, re-reading on a lost race;
    it leaves the pointers and objects in place;
  - drop (`--drop`): refuses a trail whose header does not already carry its stored context's fold, then removes `context_s3` and `native.context_s3` under the same condition;
  - `--dry-run` reports, one JSON line per trail, what either would write, without writing;
  - `--all` lists trails, so it needs a token holding read as well as administer.

Landing 2 (switch) moves writers and readers and needs no deploy:

- the writers change above;
- `rewind` renders from the header alone, and `RecordedAdapter.conversation_records` stops calling `get_launch_context`;
- `transfer` carries the header alone:
  `copy_trail` passes no launch context and `NetworkStore.begin_import` sends `{header}`.

Landing 3 (contract) deletes what is transitional:

- the fold and its callers, the route, `TrailsStore.get_launch_context`, the `launch_context` import argument and payload key, and the context comparison in `verify_same_import`;
- `DynamoStore`'s `context_s3` handling (the storage attribute, the pointer on `_header_item`, the object removal in the admin delete), and `trails fold-contexts` with its routes;
- the blaze `body` becomes exactly `{records}` for both harnesses and the import payload exactly `{header}`, so an old writer or importer is refused by the shape rule rather than by code naming the retired field.

What stays is the display of `launch_context`, the one mention of legacy data the Requirement keeps.
The admin delete's manifest records the header, so it now also keeps a trail's archived records, which it never took from the stored context object.

### Claude sessions and `AGENTS.md`

Claude Code 2.1.280 loads `AGENTS.md` only through its `agents-md` plugin, available while the remotely served flag `tengu_agents_md_mod` is on;
a process reads the flag once, and ride seeds every new workspace a fresh `.claude.json` without cached flags, so a first session often starts before the flag arrives.
No setting enables one flag in public builds:
the per-flag override `CLAUDE_INTERNAL_FC_OVERRIDES` is compiled to a no-op there.

- Ride's session settings set `DISABLE_TELEMETRY=1` in their `env` (`ride/ride/claude/claude_config.py`), so Claude Code reads no remote flag, cached ones included, and every flag holds the pinned release's default.
  Verified at 2.1.280: from a config whose cached flags have the AGENTS.md flag on, the setting kept `AGENTS.md` unloaded in 2 of 2 runs, while the same config without it loaded `AGENTS.md`; the `prompt_snapshot` is still recorded.
- A prompt file in `bro/prompts/shared/` tells a claude session, under `{{when #harness = claude}}`, to read the repository's root `AGENTS.md` if it exists and is not already in its context.
  The read lands in the trail as a tool call and its result.
- A live probe in the `llm` stage (`ride/ride/claude/*_llm_test.py`, on the pinned Claude Code) runs one session the way `ride solo` does
  — ride's settings and appended prompt, print mode over stream-json —
  in a scratch repository holding an `AGENTS.md` and no `CLAUDE.md`, records its transcript through the claude recorder into a store, and asserts the trail's messages:
  no `instructions` notice names `AGENTS.md` (Claude Code did not load it itself);
  a read of `AGENTS.md` carries its content;
  the `prompt_snapshot` notice carries ride's appended prompt.
  The checklist for moving the Claude Code pin (`ride/ride/workspace/AGENTS.md`, `build_context.py`) runs `run-tests --only llm`, so a release that starts loading `AGENTS.md` itself, or stops recording either, is noticed at the bump.

### Parties

| Party | Code it runs | Until |
|---|---|---|
| trails server (ECS, 2 tasks, rolling deploys) | the image the user deploys | each deploy; two versions serve together while one rolls |
| Dynamo `trails-v2` / `trail_steps_v2`, the S3 bucket | data | the backfill folds it, the cleanup drops the pointers and objects |
| claude and bro recorders | the session's runtime bundle | the bundle's root ends or is resumed |
| `rewind`, `trails`, `benchmark import-trails` | the invoking installation or bundle | its next update |
| ppp (pins `42087807`), kap (pins `98808b59`) | their pinned bro | their next `[[bump bro]]` |
| hosts' local stores, retained benchmark trial stores | data | not kept; their stored contexts are dropped |

### Mixed versions

| Pairing | Works | How |
|---|---|---|
| old writer → landing-1+ server | yes | the blaze folds `body.launch_context`; an attach lifts its git record |
| new writer → pre-landing-1 server | no | the server refuses the unknown `git` field (400: the claude recorder goes FAILING, a bro run fails to start); hence expand first, and landing 1 is the rollback floor once switched writers run |
| old reader → new data | yes, until landing 3 | the route answers from the header; unknown header fields are ignored |
| new reader → old data | yes | readers switch in landing 2, after the backfill folded every stored context |
| old importer → new headers | yes | `git` and `launch_context` pass `importing.imported_header` as recorded (it checks only the fields it rebuilds a blaze from) |
| old importer → landing-1+ server | yes, until landing 3 | the carried `launch_context` is folded |
| new importer → pre-landing-1 server | no | refused `git`; a rollback-only case |
| new writer → a local store an older bundle reads | no git state shown | the older reader looks for a `context.json` no new writer writes; local stores are not kept |

An old `rewind` refusing claude trails from the current server (`notification must be a string or content-block list`) predates this change (#756).

### Stored data

- Dynamo:
  after the landing-1 deploy has rolled out, `trails fold-contexts --all` folds every stored context into its header, after an on-demand table backup and a dry run.
  The fold only adds `git` and `launch_context`, which no old reader or writer depends on the absence of, so it needs no rollback.
  A second dry run must report nothing left to fold.
- The S3 objects (`trails/<id>/context.json`, `trails/<id>/context-<stamp>.json`, and the July migration's `trails/claude/<id>/launch-context.json`):
  untouched until the cleanup, which follows landing 2.
  The cleanup's dry run checks every object against its folded header and stops on a mismatch, naming the trail;
  the objects are then synced into a local archive the user keeps (about 40 MB), the pointers dropped, and the objects deleted by key pattern, orphans included.
  After the pointers go, nothing reads the objects, not even a server rolled back to landing 1 or 2, which answers the route from the header when a trail holds no pointer.
- `context.json` files in local stores:
  left in place and unread from landing 1.

### Rollout

1. Landing 1 (expand, plus the `AGENTS.md` stage) → the user deploys the trails server → the user runs the backfill.
2. Landing 2 (switch), no deploy → ppp and kap move their pins past it → old workspaces end or are resumed.
3. The cleanup, by the user.
4. Landing 3 (contract) → the user deploys it, once ppp and kap are past landing 2, no live runtime bundle on any host predates landing 2, and the cleanup is done.

Deploys use the `aws`, `github` and `infra` credentials, and the backfill and cleanup a trails token holding read and administer.

Rollback:

- a landing: a revert on `master`;
- the server: `deploy.sh` at an older commit does not roll the image back, since the build skips an existing commit image without moving `latest`;
  a rollback retags `latest` to the previous commit's image before the service stack deploy, or points the ECS service at the previous task definition;
  once switched writers run, the floor is landing 1;
- the backfill: none needed; the table backup is for disaster only;
- the cleanup: the archive syncs back; the pointers need no restoring, since every reader answers from the header.

### Rejected alternatives

- A header format bump with an upgrade:
  the stored context is not in the header, so no in-memory transform can fold it, and a backfill is needed anyway;
  a newer format is refused by every format-1 reader (a rolled-back server, ppp's and kap's pins, frozen bundles), and rows share the format number, so a migrate-on-write would rewrite every row of every reopened trail.
- `location.branch` / `location.base_sha`:
  `location` says where the session ran;
  an old importer refuses unknown `location` keys, and an old writer's attach restamps `location` wholesale, erasing the git state.
- A read-only reader path over the stored contexts: the route, the store read, the import carriage and the storage would all stay.
- Header references into the content-addressed blob store (37 MB of text dedupes to 6.9 MB):
  the tool-blob store would become a generic one, the begin-import wire would carry blobs, and `rewind show` would fetch per old trail.
- Dropping the records the model never received:
  code cannot tell kap's 8 `AGENTS.md (root)` records, model input through kap's `CLAUDE.md` symlink, from the rest, and 162 MCP lines are the only record of which persona ran.
- Folding a local store's `context.json` on read, or migrating local stores: either keeps legacy code or adds a step per host, for data nobody keeps.
- Writing the S3 object as well during the transition: it serves only a rollback below landing 1, which switched writers forbid anyway.
- Two landings, the switch and the contract together: ppp, kap and every live older bundle would stop recording, and their `rewind show` fail, at that deploy.
- Keeping the S3 objects for a soak, or forever: nothing reads them once the pointers go, and the archive covers recovery.
- For `AGENTS.md`:
  seeding the flag into the cached features fixes only the start-up race and leaves the flag Anthropic's;
  a `CLAUDE.md → AGENTS.md` symlink per workspace tree adds a file to every tree;
  pasting `AGENTS.md` into the appended prompt bloats every prompt, misses edits made during the session, and would duplicate it once a release loads it natively.
- Rewriting an ssh `url` into https: the pairing is a hosting convention (GitHub, GitLab), not a git rule, and ride's clone step already does it for GitHub.

### Risks

- Old claude headers grow by the archived records:
  1,027 trails, median 28.5 KB, at most 63 KB, 37 MB in all, plus ppp's and kap's until their pins move.
  That is well under Dynamo's 400 KB item limit, but the five `trails-v2` indexes project every attribute:
  a list page over those dates holds fewer headers (Dynamo stops a page at 1 MB), and a write to such a trail costs about 30 times a small header's.
- The `AGENTS.md` read depends on the model following the line:
  the live probe holds it, and each session's trail shows the read.
- With remote flags off, every flag-gated feature holds the pinned default, including ones Anthropic enables later;
  the probe at each pin bump covers what the trails depend on.
- The backfill needs a trails token holding read and administer; whether one exists is for the user to confirm.
- A malformed stored context is refused by the fold, naming the trail; the service audit found none.
