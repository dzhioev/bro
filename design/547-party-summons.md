# Party summons and one launcher for ride

The design for [add the --here summon mode and unify ride's launcher around it](https://github.com/dzhioev/bro/issues/547), reviewed as a document through a temporary pull request:
the settled text replaces the task's `## Design`, and this file is not merged.
Settled with the user in an attended bro-dev session on 2026-09-11 (summon `01m285zfxa-991r82ze-g68t4gps`, trail `01m2862xjr-g4yacds7-f81s3bwb`) on #131,
and reworked with the user in the review session of 2026-09-12 (trail `01m29ek9pm-t971adkf-7x52ph2t`), where the vocabulary and the policy below were settled.
Nothing below is implemented.

**Vocabulary.**

- A **ride** is what one `ride solo|along` command starts:
  the root session and every session summoned beneath it, transitively, wherever each runs.
  A ride has one broker, one runtime bundle, one summon audit, and one artifact store;
  the docs say "one bundle per ride" and "the ride's broker", never "session" for the tree.
- `ride` is the launcher:
  it opens a workspace, runs the root in it, and supervises the ride.
  `do-ride` is the session executable:
  it runs one session in a prepared workspace, and every session runs it, whichever launcher put it there.
- A **workspace** is a named directory holding a writable tree and the records of the sessions that ran in it.
  Its **isolation** is `boxed`, a container of its own around the tree, or `unboxed`, the tree as a directory on whatever `ride` runs on, a foreign container included.
  Isolation is fixed at creation and replaces `WorkspaceKind`;
  "container" stays the word for the box itself.
- A **session** is one agent run.
  The sessions sharing one workspace are its **party**:
  the session that opened the workspace started the party, later ones joined it, and all are party members.
  The root starts the ride's first party.
- A summon either **starts** a party, a workspace of its own for the child, or **joins** the summoner's, running the child as a process beside it:
  `docker exec` into the party's container when it is boxed, a subprocess in its tree when it is unboxed.
  The lowering is keyed by the summoner, so nesting is uniform:
  a child that joins a boxed party's member runs in that party's container.
- **Permits** are what a session may do about parties, carried in the unified `--grant`/`--revoke` grammar beside credentials and `@bro` targets.
- The process/thread analogy holds:
  the workspace is the process, the session the thread, a started party a new process, a joined one a new thread.

**The summon request.**
The request carries `party` (`start` | `join`) and, for a start, `isolation` (`boxed` | `unboxed`):
`summon --start | --join`, `summon --boxed | --unboxed`, and the `bro::summon` fields `party` and `isolation`.
Isolation beside `--join` is refused as contradictory, since a joined child inherits its party's.
A request naming no party act starts a party, boxed when the requester holds `:party.start.boxed`, else unboxed when it holds `:party.start.unboxed`;
where it holds neither, the request is refused naming the permits it does hold, and is never turned into a join.
Joining is always explicit.
`into` is refused for a join, since a child sharing the tree has no base to check out;
`share` works for a join and links into the party's view.
A manual summon (`--manual`, launched by the user with `ride along --summoned`) is a root-shaped launch:
`party` and `isolation` are refused on it as launch-owned fields, like `hold` and `harness`;
the requester needs a start permit, either one, since the launch starts a party;
and the user's own launch settles the isolation, as it settles the rest of the session's shape.
That is a deliberate exception to the per-session placement check:
the launch runs in the human's own environment under the human's own scope, so the requester's boxed or unboxed permit does not bound it, only whether a party may be started at all.
Nothing probes the environment for placement:
the request says what it wants, the permits say what is allowed, and a boxed start on a ride whose daemon preflight fails (see "Roots anywhere") fails at spawn with that reason.
A detached join shares a working tree with a summoner that keeps working;
that is the summoner's call, and the join permit is what an operator withholds to prevent it.

**Permits.**

- The leaves are `:party.start.boxed`, `:party.start.unboxed`, and `:party.join`;
  the marker `:` puts them in the unified grammar beside a credential (`github`, `github+dev`) and a bro (`@reviewer`).
  A grant or revoke names a leaf;
  `:party` or `:party.start` fails naming the leaves, so nothing grants unboxed starts by accident.
- The framework seed is `party.start.boxed`, today's behaviour.
  The layers apply in order:
  the seed, the project's `[tool.bro] grant`/`revoke`, the host config's `defaults`, the matching `projects` entries (URL then path), their `bros.<bro>` entries, and the launch's `--grant`/`--revoke`;
  a summon request's `grant`/`revoke` layer on the child's result.
  Config layers are idempotent, as `bros.<bro>.grant` already is for credentials, so a shared dotfile restating a project's grant is not an error;
  the flags stay strict, so a no-op grant or a revoke of nothing held fails the launch.
- `[tool.bro] grant` and `revoke` take the full grammar:
  a bare credential kind joins every session's required tier for the project (an instance-spelled name is refused there, since which instance backs a kind stays out of the repo), `@bro` widens the allow-list, and `:permit` grants a permit.
  The host config accepts `grant` and `revoke` at `defaults`, at `projects.<identity>`, and at `projects.<identity>.bros.<bro>`, in the same grammar;
  today only `bros.<bro>.grant` exists, and only for credentials.
- Permits are per session and bounded down the chain like `@bro`:
  the root's set is computed at launch and recorded in its peer-facts row;
  a summoned child's set is its own seeds (the config layers for its bro on the ride's attachment) under its request's grant/revoke;
  a request may grant its child only permits the requester holds itself;
  and the control checks the request's placement against the requester's row and denies with the reason.
  `ride resume --grant/--revoke` adjusts a recorded set, and `RIDE_PERMITS` carries a session's own set into it, read by `ride banner` beside `may_summon`.
  A manual summon's pending record fixes the child's permits like its `may_summon`, and the `--summoned` launch refuses `:permit` overrides as it refuses `@bro` ones.
- Granting `:party.start.unboxed` to a boxed ride lets its sessions run processes on the host as the launching user, which is why the leaf is never in the seed and is always spelled out in full.

Examples, a `pyproject.toml`, a `~/.bro.json`, and launches:

```toml
[tool.bro]
default = "bro-dev"
grant = [":party.join"]                 # this project's sessions may have joiners
```

```toml
[tool.bro]
default = "terminal"
grant = [":party.start.unboxed"]        # a project that runs without Docker
revoke = [":party.start.boxed"]
```

```json
{
  "defaults": {"creds": ["github+dev", "trails+write"]},
  "projects": {
    "https://github.com/me/bro": {
      "creds": ["brog+github"],
      "grant": [":party.join"],
      "bros": {
        "bro-eyebro": {"creds": ["github+reviewer"], "revoke": [":party.join"]}
      }
    },
    "/home/me/projects/bro": {"grant": [":party.start.unboxed"], "revoke": [":party.start.boxed"]}
  }
}
```

```console
ride along --grant :party.join bro-dev
ride solo --unboxed --revoke :party.start.boxed --grant :party.join --keep --harness bro terminal 'task'
summon reviewer 'check the diff' --join
summon reviewer 'check the diff' --grant :party.join
```

**What a party member keeps, and what it shares.**
A joined session keeps everything a started one has except a workspace of its own:
the scope the host hydrates from its store (kinds the summoner does not hold included), allow-list, permits and depth enforcement, journal and audit;
its own channel and broxy, its own records and trail, either harness, `llm`, `hold`, `timeout`, `share`.
The workspace is the isolation boundary, not the process.

- Records:
  a joined session's records live in its party's workspace under `party/<channel>/`, the channel being the name a started child's workspace would have had (`broker-<channel>`);
  the directory holds a `session/` and a `claude/` of the shape the workspace's own have;
  the party's first session keeps the workspace's records where they are today.
  A boxed workspace bind-mounts `<workspace>/party/` at `/var/ride/party` when its container is created, so a member's dirs, created later, are reachable inside:
  `RIDE_SESSION_DIR=/var/ride/party/<channel>/session` and `CLAUDE_CONFIG_DIR=/var/ride/party/<channel>/claude`, the absolute host paths for an unboxed party.
  The trail pointer and the recording health signal are therefore per session;
  the peer-facts row carries the member beside the workspace, so attribution reads the right pointer, and the audit's `summoner` names `{workspace, member?, bro, trail_id?}`.
- Credentials:
  the ride hydrates the member's own scoped store and delivers it the way the party's isolation does:
  `docker cp` into the running container's own layer at a per-member path, re-owned as the entrypoint re-owns the root's, for a boxed party, and a private temp directory for an unboxed one;
  `BRO_STORE` names it, and `do-ride` installs the hooks into a per-session directory the env names.
- Claude state:
  the ride provisions the member's claude dir host-side before the exec, exactly as it does for a started party, so `do-ride` finds it done;
  `do-ride`'s own seeding is the fallback for a launcher without host state (see "Roots anywhere").
- Lifecycle:
  an unboxed member is a plain subprocess handle.
  A boxed member is a `docker exec -i -u ride -w /workspace` client running `env -i` with an explicit baseline (`HOME`, `PATH`, `TERM`, `LANG`) and the member's env,
  so nothing of the party's first session leaks into it and it starts from the same closed snapshot a started child gets.
  The client's death does not end the process inside, so the exec'd command writes its pid and start time under the member's session dir before it execs `do-ride`;
  the handle reports started only once that file or the client's exit is seen, and a kill arriving earlier waits for the same handshake;
  the kill goes through `docker exec <container> kill` after checking the recorded start time against `/proc`, and `do-ride` removes the file at exit, so a late kill finds no target and never a reused pid.
  Root exit and timeouts use that.
- A party ends with its first session:
  the ride kills members still running, loudly, the way root exit kills in-flight children, then removes a throwaway workspace.
  A member's records are removed after its clean exit and kept for inspection otherwise, like a throwaway workspace;
  a member is not resumable (`ride resume <workspace>` resumes the party's first session), and its trail is its recovery record.
- Artifacts:
  a party has one view, the one mounted at `/var/ride/artifacts` when a boxed workspace is created, or the workspace's own `artifacts/` dir when unboxed;
  a member mints and gets through its party's view, and a share to a member links into that view.
  Sharing the tree already makes a member's files the party's.
- Workspace provisioning (`setup.sh`, the persona's `provision_workspace`) is the party's first session's;
  a member runs none, and its git identity is its own env.
- The banner and the session prompt fragment state party membership:
  a joined session is told it shares its tree with the session that summoned it and works beside it.
- Depth counts joined children as generations, like started ones.

**Roots anywhere.**
`--boxed`, the default, needs a Docker daemon that can bind-mount the launcher's filesystem, the daemon of this machine or a Desktop VM sharing its home;
a ride preflights that once, before its first boxed launch or start, by mounting the runtime root into a throwaway container of the runtime image and reading back a nonce written there,
and fails naming the daemon when it cannot, whatever the machine.
The one nonce covers every bind source, because every bind source of a boxed launch lies under the runtime root:
the tree, the records, the `party/` dir, the artifact views, and the local trails root, which is `runtime_base()/trails` outside a managed container, while the runtime volume is the daemon's own.
The launch asserts that on each mount and refuses a source outside the root rather than bind it blind.
`--unboxed` works wherever `ride` runs, a foreign task container included, so the in-container refusal on `/.dockerenv` goes.
The runtime invariant stays:
one runtime bundle per ride.

- `--runtime-bundle PATH` names a materialized runtime:
  `PATH/venv`, with `do-ride` and the other session commands in its `bin`, and `PATH/bin`, the shim farm that leads the session PATH.
  A frozen bundle's host materialization (`<runtime-root>/runtime/<hash>/host/`) has that layout, and so does the container volume at `/var/ride/runtime`.
  The launcher takes it as given instead of freezing one, and re-executes itself from `PATH/venv/bin/ride` unless it already runs there, so the launcher, the broker, and every `do-ride` of the ride run one runtime;
  `ride resume` re-executes the same way from the recorded path.
  A given runtime carries no manifest to materialize a volume from, so a boxed root and a boxed start from such a ride are refused naming that.
  The benchmark bundle is built in that layout and named to `ride` by the Harbor agent (#131).
- `--tree PATH` runs an unboxed detached root in an existing directory as its tree instead of a fresh empty one;
  the records still live under the runtime root (`$XDG_DATA_HOME/ride`), which is how the trial runs in the task's directory while Harbor collects the records under `/logs/agent`.
  The workspace record holds the tree's absolute path and `Workspace.tree` returns it, so the peer facts, the artifact source paths, the lowering, `ride list`, and `ride resume` see it with no case of their own.
  The path must be an existing directory outside the runtime root;
  one workspace records it at a time, a second launch naming it being refused with the first's name;
  a resume fails when it is gone;
  and removal (`--drop`, `ride clean`, with or without `--force`) removes the records and never the tree, such a workspace being clean when its last session ended cleanly, since the tree is not ours to judge.
- The trial command becomes `ride solo --unboxed --tree <task dir> --runtime-bundle <bundle> --revoke :party.start.boxed --grant :party.join --keep --harness <h> [--llm :<recipe>] terminal <task>`,
  under the agent's existing `setsid` wrapper, and the `terminal` bro's summons say `--join`.
- Explicit facts replace probes:
  the launcher exports `RIDE_ISOLATION`, which the banner renders as `isolation:` in place of the `/.dockerenv` probe, and `RIDE_PERMITS`;
  a launcher without a host `~/.claude.json` seeds a session's claude state without the account identity.

**do-ride.**
`ride solo|along --in-place` becomes the `do-ride` console script, declared in `bro.session_commands` so every runtime carries it:
`do-ride solo|along --workspace NAME --harness H [--resume] [--repo R] --hold HOLD [--llm L] <harness flags> <bro> [prompt] [-- args]`, with a parser of its own and none of the machinery-flag refusals.
The split follows one rule:
per-session steps belong to `do-ride`, per-workspace steps to whoever creates the tree.

- `do-ride` owns the session env (git identity, `RIDE_BRO`, `BRO_HOLD`, `RIDE_RUNNER_PID`, its pid file), the credential hooks (`BRO_STORE` plus `BRO_INSTALL_KINDS`, into a directory it names in the env),
  the claude state dir when none is present and its plugin seed (the image's seed in a container, the host install's plugins on a host, behind the guard file both use today),
  the session broxy from `BROKER_UPSTREAM` in both isolations, and the harness's runner.
  All of it is idempotent, so a launch that pre-provisioned finds the work done.
- The container entrypoint keeps the per-workspace steps, the uid remap, `safe.directory`, the project venv link, and `setup.sh`;
  it no longer installs hooks, seeds plugins, or starts a broxy.
  The unboxed launch keeps `setup.sh`, the one thing left of `worktrees.py`.
- The entrypoint-versus-inner unification therefore joins this orchestration for its per-session half;
  store delivery (`docker cp` versus a materialized directory) stays the fork left for later.

**Unifications this needs.**

- One launcher for both isolations, replacing `ride/session.py`'s separate container and host paths, used by `ride` for roots and by the summon lowering for started parties, plus one new operation, joining a party.
  The lowering can then start unboxed parties from any ride (a process child beside the docker one) and start boxed ones from an unboxed root, as today.
- Every attached tree is a clone (`ride/workspace/clones.py`):
  a git worktree cannot work inside a container while a clone works anywhere, and a local clone hardlinks its objects.
  `worktrees.py` retires except for its `setup.sh` run, and the word goes with it:
  `WorktreeWorkspace` becomes `UnboxedWorkspace`, `ride list` says boxed and unboxed, the reference doc likewise.
  A workspace whose tree is a legacy worktree (a `.git` gitfile) is refused with the `ride clean --force <name>` that recreates it, and `ride clean` keeps the worktree release for those trees.
- The session branch becomes `workspace-<name>` for new workspaces;
  existing records keep the branch they store, and the session context reads it from the metadata instead of deriving it.
- `ride list` badges:
  `[o]` boxed and `.o.` unboxed with a live session, `[-]` and `.-.` idle;
  "idle" replaces "abandoned", which named every kept workspace between sessions.
- The workspace record becomes `workspace.json`, holding `isolation` (`boxed` | `unboxed`) and, when given, `tree`.
  The first outer `ride` command after upgrading migrates every workspace once, under the runtime state migration's lock and liveness preflight:
  `meta.json` becomes `workspace.json` with `kind` translated (`container` → `boxed`, `worktree` → `unboxed`), and `resume.json` has its `host` translated to `isolation`;
  the loaders stay strict and read only the new shapes.
- The peer facts record each peer's workspace and member;
  the lowering of a join reads the summoner's workspace record for its isolation.
- An unboxed session's scoped store and hook files move to a private temp directory removed at session end, so a collected records tree never carries a secret.
- The artifact store, the summon audit, and the docs key on the ride (the root's workspace name), and say so.
- `--host` becomes `--unboxed` on `ride solo|along`, in `ride resume`'s recorded spec, and on `dive-in`;
  the omitted hold of an unboxed `along` stays `guided`, and `--raw` stays boxed-only.

**Rollout and mixed versions.**
Within a ride every process runs one runtime:
the launcher re-executes itself from a given `--runtime-bundle`, and a manual child re-executes from the ride's runtime its token names, so the launcher, `do-ride`, the summon wire, and the peer facts never skew.
The contracts that cross installations:

- Workspace records (`workspace.json`, `resume.json`, the `party/` dir, the branch):
  the one-shot migration converts them, after which an older installation sharing the runtime root sees no workspaces at all and would write its own record over an existing directory if asked to, as the previous root migration already left it.
  Upgrade every checkout that runs `ride` against one runtime root together, and do not run an older one against a migrated root;
  a downgrade needs `ride clean --force` of every workspace first.
- `resume.json`:
  the migration above translates `host` to `isolation` once and writes the new fields (`tree`, `runtime_bundle`) empty, so the strict loader stays;
  a resume validates a recorded tree or runtime path before it starts.
- The manual-summon token:
  `PendingSummon` names the ride's runtime, the bundle hash or the given path, beside the child's `permits`, and the ride materializes the runtime's host half when it mints the token;
  the command it relays names that runtime's own executable, `<runtime>/venv/bin/ride along --summoned <token> <target>`, so the bootstrap needs no installation of the user's to read the token.
  A `ride` of another installation typed by hand reads the runtime field first and re-executes itself from it when it can, and an older one fails loading the record, which is the refusal;
  either way the child runs the code of the ride that minted the token, and the protocol revision check retires with the skew it guarded.
- `[tool.bro] grant`/`revoke` and the host config's new lists:
  an older `ride` fails the launch on the unknown key, so upgrade `ride` before adding them to a project or a dotfile.
- The benchmark bundle (#131) follows this work:
  it needs the materialized layout and the `do-ride` command.
- The summon and artifact audits key the ride under `ride`, where rows written before the upgrade carry `session`;
  they are append-only files nothing reads back, so a file spanning the upgrade holds both keys and a reader takes either.
  Trails only gain fields (`member`, party facts).

**Settled in the review.**

- The run-in-this-directory flag is `--tree PATH`;
  the runtime-bundle pointer is `--runtime-bundle PATH`.
- The per-session half of the entrypoint-versus-inner unification joins this orchestration through `do-ride`;
  store delivery stays for later.
- Vocabulary:
  ride, do-ride, party, start/join, isolation boxed/unboxed, permits;
  `--here` and `--host` are gone.
- Policy:
  the two tri-state settings became permits in the grant grammar, and an unmarked request starts and is never converted to a join.
