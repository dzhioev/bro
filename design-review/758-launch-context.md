# #758 design review

A design under review, never to be merged:
the pull request carrying this file closes unmerged once the design is settled, and the design itself lives on https://github.com/dzhioev/bro/issues/758 under `## Design`.
Its `## Goal`, `## Requirement` and `## Research` sections are the context it answers.

## Design

The git state moves into the header as a `git` object, and every stored launch context is folded into its trail's header once, so the separate launch-context channel can go.
Three landings on `master` carry it, with a trails-server deploy after the first and the last:
an expand landing that accepts both shapes, a switch landing that moves writers and readers, and a contract landing that deletes everything transitional.
A stage in the first landing hands claude sessions the root `AGENTS.md` through the harness, without depending on Claude Code's remote feature flags or on what the persona's tools allow.

### Header vocabulary

Both fields are additive, so the trail format stays 1.

- `git: {repo, url, branch, base_sha}` — the git state of the session's attached tree, a top-level header field beside `location`.
  - `repo` is the attachment identity ride publishes as `RIDE_REPO`: the checkout path, or the URL of a URL attachment.
  - `url` is the session tree's `origin` when it names a network remote (an scp-form remote, or a `scheme://` URL other than `file://`).
    A local path or `file://` origin leaves it out.
    It is no rewrite of its own:
    ride's clone step already turns a GitHub scp origin into its https form when it sets the tree's `origin` (`ride/ride/workspace/clones.py`).
  - A URL in either field passes one named sanitizer in `bro.base.git_url` before it is recorded:
    `normalize_git_url`, then the userinfo, the query and the fragment dropped
    — so a token in `https://user:token@host/repo` or `https://host/repo?token=…` never reaches a header or the indexes that project it.
    The scp form keeps its user, which has no password field.
  - `branch` and `base_sha` are `RIDE_BRANCH` / `RIDE_BASE_SHA`.
  - Each key is an optional non-empty string, an unknown key is refused, and an empty object is refused:
    a trail without git state has no `git` at all
    — a detached session, a run outside a managed session.
    `model` holds that one validator, and both entries run it:
    a blaze through `BlazeRequest`, and an import through the `BlazeRequest` that `importing.imported_header` builds from the recorded header, which passes `git` to it.
  - A blaze writes it and an attach restamps it latest-wins beside `location`, `version` and `hold` (`backends.attached_header`), so it names the latest launch's git state.
    The stored record it replaces always named the minting launch's.
  - A trail recorded before the change gets `branch` / `base_sha` from its stored record and no `repo` or `url`, which were never recorded.
- `legacy_launch_context: [...]` — history only:
  the trail's stored launch context, verbatim and whole, its git record included, in stored order
  (the appended system prompt, the MCP servers line, the root instructions file, the git state).
  It is left out where the stored context was a single git record carrying only `branch` / `base_sha`, which `git` holds entirely.
  It exists on trails recorded before the change, and on trails that recorders older than #756 open during the transition (ppp's and kap's pins, bundles frozen before #756).
  No writer sends it and no blaze accepts it;
  an import carries it as recorded.
  It is opaque:
  nothing validates or interprets it but the display, so no code for the legacy shape outlives the rollout beyond that rendering.
- `rewind show` lists `repo`, `url`, `branch` and `base sha` among the header fields beside `workspace` and `host`,
  and renders `legacy_launch_context` as one more header field through the display's YAML rendering, long texts as literal blocks, which `rewind grep` searches like any rendered line.
  `LaunchContextEntry`, `RecordKind.LAUNCH_CONTEXT` and the `SESSION CONTEXT` block go.

### The fold

The fold turns a stored launch context into header fields; it is transitional and leaves with the contract landing.

- The context must be a list of objects with at most one `git/state` record;
  anything else is refused, naming the trail.
  The service holds 1,169 stored contexts, every one a list with at most one git record (`## Research`, "Fold rule inputs").
- The git record's `branch` / `base_sha` become `git` when the header has no `git`.
  A header already carrying `git` keeps it, since an attach after the mint restamped it with a later launch's state.
- The whole list becomes `legacy_launch_context`, unless it is a single git record carrying only `branch` / `base_sha`.
  A header already carrying `legacy_launch_context` must carry this same list;
  a difference is refused, naming the trail.
- The context the route rebuilds from a header is `legacy_launch_context` verbatim where the header has one;
  otherwise a single `git state at launch` record with `git`'s `branch` / `base_sha`, where the header has `git`;
  otherwise none.
  An old reader thus sees a folded trail's stored context unchanged, in stored order, with its one git record.
- Folding the context rebuilt from a folded header into that header changes nothing, and neither does folding a context twice.

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
- `begin_import` folds a carried `launch_context` payload (an old importer) into the imported header and stores no context;
  `verify_same_import` compares the folded forms of the stored and the carried trail.
- `GET /v1/trails/{id}/context` answers old readers with the context rebuilt from the header;
  a Dynamo trail still holding a pointer is folded first, so the answer is the same before and after the backfill.
- `LocalStore` stops writing and reading `context.json`:
  local stores are not kept, so an old local trail loses its stored context, and a layout imported through `transfer.LayoutSource` arrives without one.
- `DynamoStore` gains two transitional administration operations,
  forwarded like `recompute` / `check` / `relink` (concrete `NetworkStore` methods and `/v1/admin/*` routes, answering 501 on other backends),
  and the `trails` CLI drives them as `trails fold-contexts [--dry-run] [--drop] (--all | TRAIL_ID…)`:
  - fold: reads the object a trail's `context_s3` or legacy `native.context_s3` names, folds it, and writes `git` / `legacy_launch_context` with an update conditional on the fields it read, re-reading on a lost race;
    it leaves the pointers and objects in place;
  - drop (`--drop`): refuses a trail whose header the fold would still change, then removes `context_s3` and `native.context_s3` under the same condition;
  - `--dry-run` writes nothing and reports one JSON line per trail:
    its pointer keys, what the fold would add, and whether the header already carries the fold;
  - `--all` lists trails, so it needs a token holding read as well as administer;
  - each operation is idempotent per trail, so an interrupted `--all` is re-run whole.

Landing 2 (switch) moves writers and readers and needs no deploy:

- the writers change above;
- `rewind` renders from the header alone, and `RecordedAdapter.conversation_records` stops calling `get_launch_context`;
- `transfer` carries the header alone:
  `copy_trail` passes no launch context and `NetworkStore.begin_import` sends `{header}`.

Landing 3 (contract) deletes what is transitional:

- the fold and its callers, the route, `TrailsStore.get_launch_context`, the `launch_context` import argument and payload key, and the context comparison in `verify_same_import`;
- `DynamoStore`'s `context_s3` handling (the storage attribute, the pointer on `_header_item`, the object removal in the admin delete), and `trails fold-contexts` with its routes;
- the blaze `body` becomes exactly `{records}` for both harnesses and the import payload exactly `{header}`, so an old writer or importer is refused by the shape rule rather than by code naming the retired field.

What stays is the display of `legacy_launch_context`, the one mention of legacy data the Requirement keeps.
The admin delete's manifest records the header, so it now also keeps a trail's legacy launch context, which it never took from the stored context object.

### Claude sessions and `AGENTS.md`

Claude Code 2.1.280 loads `AGENTS.md` only through its `agents-md` plugin, available while the remotely served flag `tengu_agents_md_mod` is on;
a process reads the flag once, and ride seeds every new workspace a fresh `.claude.json` without cached flags, so a first session often starts before the flag arrives.
No setting enables one flag in public builds:
the per-flag override `CLAUDE_INTERNAL_FC_OVERRIDES` is compiled to a no-op there.
A line asking the model to read the file is no guarantee either:
the `lead` persona blocks every file tool (`dev/bros/lead/__init__.py` blocks `claude.FILES`), and a model that skips the line loses the file silently.

- Ride's session settings set `DISABLE_TELEMETRY=1` in their `env` (`ride/ride/claude/claude_config.py`), so Claude Code reads no remote flag, cached ones included, and never loads `AGENTS.md` itself.
  Verified at 2.1.280: from a config whose cached flags have the AGENTS.md flag on, the setting kept `AGENTS.md` unloaded in 2 of 2 runs, while the same config without it loaded `AGENTS.md`; the `prompt_snapshot` is still recorded.
  Every other flag holds the pinned release's default too, a deliberate choice so a pinned release behaves the same until the pin moves:
  at 2.1.280, 114 served values differ from their built-in defaults among the 277 flags read with a literal default, most of them remote-session, IDE, notification and marketplace plumbing.
- A `SessionStart` hook in ride's session settings, beside the hooks `ride/ride/claude/claude_argv.py` already installs, hands Claude Code the tree's root `AGENTS.md` as `additionalContext` when the root holds one and no `CLAUDE.md`.
  It needs no tool and no model compliance, so it reaches every persona.
  Verified at 2.1.280 in print mode over stream-json: the model received the text, and the transcript recorded it as a `hook_additional_context` attachment, which the trail projects as a notice.
  Per Claude Code's hook documentation, `SessionStart` also runs on a resume and after a compaction.
- A live probe in the `llm` stage (`ride/ride/claude/*_llm_test.py`, on the pinned Claude Code) runs one session the way `ride solo` does
  — ride's settings and appended prompt for the `lead` persona, print mode over stream-json —
  in a scratch repository holding an `AGENTS.md` and no `CLAUDE.md`, records its transcript through the claude recorder into a store, and asserts the trail's messages:
  a `hook_additional_context` notice carries the `AGENTS.md` content;
  no `instructions` notice names `AGENTS.md` (Claude Code did not load it itself);
  the `prompt_snapshot` notice carries ride's appended prompt.
  The checklist for moving the Claude Code pin (`ride/ride/workspace/AGENTS.md`, `build_context.py`) runs `run-tests --only llm`, so a release that starts loading `AGENTS.md` itself, or stops recording either, is noticed at the bump.

### Parties

| Party | Code it runs | Until |
|---|---|---|
| trails server (ECS, 2 tasks, rolling deploys) | the image the user deploys | each deploy; two versions serve together while one rolls |
| Dynamo `trails-v2` / `trail_steps_v2`, the S3 bucket | data | the backfill folds it, the cleanup drops the pointers and objects |
| claude and bro recorders | the session's runtime bundle | the bundle's root ends or is resumed |
| `rewind`, `trails`, `benchmark import-trails` | the installation or bundle that invokes them | its next update; landing 3's gate updates every installation on each host |
| ppp (pins `42087807`), kap (pins `98808b59`) | their pinned bro | their next `[[bump bro]]` |
| hosts' local stores, retained benchmark trial stores | data | not kept; their stored contexts are dropped |

### Mixed versions

| Pairing | Works | How |
|---|---|---|
| old writer → landing-1+ server | yes | the blaze folds `body.launch_context`; an attach lifts its git record |
| new writer → pre-landing-1 server | no | the server refuses the unknown `git` field (400: the claude recorder goes FAILING, a bro run fails to start); hence expand first, and landing 1 is the rollback floor once switched writers run |
| old reader → new data | yes, until landing 3 | the route answers from the header; unknown header fields are ignored |
| new reader → old data | yes | readers switch in landing 2, after the backfill folded every stored context |
| old importer → new headers | yes | `importing.imported_header` keeps unknown top-level fields as recorded, so `git` and `legacy_launch_context` pass |
| new importer → pre-landing-1 server | yes | the same pass-through; `git` is then stored unvalidated, which only a rollback below landing 1 allows |
| old importer → landing-1+ server | yes, until landing 3 | the carried `launch_context` payload is folded |
| new writer → a local store an older bundle reads | no git state shown | the older reader looks for a `context.json` no new writer writes; local stores are not kept |

An old `rewind` refusing claude trails from the current server (`notification must be a string or content-block list`) predates this change (#756).

### Stored data

- Dynamo:
  after the landing-1 deploy has rolled out, `trails fold-contexts --all` folds every stored context into its header, after an on-demand table backup and a dry run.
  The fold only adds `git` and `legacy_launch_context`, which no old reader or writer depends on the absence of, so it needs no rollback.
  A second dry run must report nothing left to fold.
- The S3 objects (`trails/<id>/context.json`, `trails/<id>/context-<stamp>.json`, and the July migration's `trails/claude/<id>/launch-context.json`):
  untouched until the cleanup, which follows landing 2 and runs from a persisted inventory:
  1. inventory: `trails fold-contexts --all --drop --dry-run` names every pointed-to key with its trail, and each must already carry its fold, or the cleanup stops for a new backfill;
     an S3 listing of the three key shapes names every object, and those no pointer names are unreferenced candidates (a losing import's object, a failed mint's), archived but not compared, since their content may legitimately differ;
     the inventory file records each key, its size and its ETag;
  2. archive: every inventoried key is downloaded into a local archive the user keeps (about 40 MB), with a SHA-256 manifest, and checked complete against the inventory;
  3. drop: `trails fold-contexts --all --drop`;
  4. audit: a fresh `--drop --dry-run --all` must report no `context_s3` and no `native.context_s3` left, or step 3 runs again;
  5. delete: exactly the inventoried keys, in batches, and nothing by pattern.

  Every step can be re-run after an interruption:
  steps 1 and 2 write nothing to the store, step 3 skips trails already without pointers, step 4 only reads, and deleting a key already gone succeeds.
  An object appearing after the inventory can come only from a server rolled back below landing 1, and shows as a new pointer at step 4, which then starts the cleanup over.
  Until step 5 nothing needs restoring;
  after it, the archive's keys upload back, and the pointers need no restoring, since every reader answers from the header once a trail holds no pointer.
- `context.json` files in local stores:
  left in place and unread from landing 1.

### Rollout

1. Landing 1 (expand, plus the `AGENTS.md` stage) → the user deploys the trails server → the user runs the backfill.
2. Landing 2 (switch), no deploy → ppp and kap move their pins past it → old workspaces end or are resumed.
3. The cleanup, by the user.
4. Landing 3 (contract) → the user deploys it once its gate holds:
   ppp and kap are past landing 2;
   no live runtime bundle on any host predates landing 2;
   every installation `rewind`, `trails` or `benchmark import-trails` runs from on each host is updated past landing 2;
   the cleanup is done.
   An installation missed after that fails loudly
   — its `rewind show` meets the removed route as an HTTP error, its import the refused payload key —
   and updating it is the recovery.

Prerequisites, user-side:

- the deploys: the `aws`, `github` and `infra` credentials;
- the backfill and the cleanup's store steps: a trails token holding read and administer;
- the table backup and the S3 steps: the `aws` credential,
  allowing `dynamodb:CreateBackup` and `dynamodb:DescribeBackup` on `trails-v2`,
  and `s3:ListBucket`, `s3:GetObject` and `s3:DeleteObject` (`s3:PutObject` for a restore) on the trails bucket;
  the bucket and the table are confirmed from the deployment configuration before any write.
  The transitional admin operations run as the trails server's own ECS task role, which needs nothing new.

Rollback:

- a landing: a revert on `master`;
- the server: `deploy.sh` at an older commit does not roll the image back, since the build skips an existing commit image without moving `latest`;
  a rollback retags `latest` to the previous commit's image before the service stack deploy, or points the ECS service at the previous task definition;
  once switched writers run, the floor is landing 1;
- the backfill: none needed; the table backup is for disaster only;
- the cleanup: per step, as above.

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
- Lifting the git record out of the legacy list: the route could not rebuild the stored order, and a record carrying `base_ref` would come back twice.
- Validating `legacy_launch_context` on import: it would keep code for the legacy shape after the rollout, for a field only the display reads.
- Folding a local store's `context.json` on read, or migrating local stores: either keeps legacy code or adds a step per host, for data nobody keeps.
- Writing the S3 object as well during the transition: it serves only a rollback below landing 1, which switched writers forbid anyway.
- Two landings, the switch and the contract together: ppp, kap and every live older bundle would stop recording, and their `rewind show` fail, at that deploy.
- Keeping the S3 objects for a soak, or forever: nothing reads them once the pointers go, and the archive covers recovery.
- For `AGENTS.md`:
  a system-prompt line asking the model to read it fails for a persona without file tools and for a model that skips it;
  seeding the flag into the cached features fixes only the start-up race and leaves the flag Anthropic's;
  the plugin's own "Project instructions" option, set as `projectInstructions` in `settings.json`, did not stop the native load at 2.1.280;
  a `CLAUDE.md → AGENTS.md` symlink per workspace tree adds a file to every tree;
  pasting `AGENTS.md` into the appended prompt bloats every prompt and misses edits made during the session.
- Rewriting an ssh `url` into https: the pairing is a hosting convention (GitHub, GitLab), not a git rule, and ride's clone step already does it for GitHub.

### Risks

- Old claude headers grow by their legacy launch context:
  1,027 trails, median 28.5 KB, at most 63 KB, 37 MB in all, plus ppp's and kap's until their pins move.
  That is well under Dynamo's 400 KB item limit, but the five `trails-v2` indexes project every attribute:
  a list page over those dates holds fewer headers (Dynamo stops a page at 1 MB), and a write to such a trail costs about 30 times a small header's.
- With remote flags off, every flag-gated feature holds the pinned default, including ones Anthropic enables later;
  the probe at each pin bump covers what the trails depend on.
- A release that loads `AGENTS.md` natively despite the flags being off would hand the model the file twice, beside the hook's copy;
  the probe's `instructions` assertion catches that at the bump.
- `legacy_launch_context` is opaque, so a malformed one arriving through an import is stored and fails only when rendered;
  only an administer-token import can carry it.
- The backfill and cleanup need a trails token holding read and administer and the AWS permissions above; whether they exist is for the user to confirm.
- A malformed stored context is refused by the fold, naming the trail; the service audit found none.
