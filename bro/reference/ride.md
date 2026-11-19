# ride

`ride` is the managed-workspace runtime.
It combines a harness implementation with a bro personality and prepares a boxed or unboxed workspace with optional typed attachments.
It hydrates the launch scope, supervises the root session and its summons, and records enough state to resume the workspace later.

The runtime is published by the `bro-ride` distribution and depends on the `bro` framework.
The framework never imports `ride`:
workspace mechanics, credential scoping, broker supervision, and bro composition remain reusable framework layers.

## Commands

> **Isolation boundary:**
> `bro run` and `bro chat` run immediately in the calling process with ambient credentials.
> They do not isolate, clone a workspace, or hydrate a scoped store.
> Use `ride solo` / `ask` for an isolated one-shot and `ride along` / `call` for an isolated interactive session.

`ask` and `call` are aliases of `ride solo` and `ride along`, with no implied flags;
`summon` and `quest` are the self-contained peer clients.

### `ride solo <bro> <prompt>`

Runs one prompt without a TTY and writes the harness reply to stdout.
The default hold is `unattended`.
The harness's diagnostics land on stderr beside the ride's own log lines,
and the session record `ride resume` needs is retained when the workspace is kept.

A launch without `-w / --workspace` receives a fresh name and removes that workspace after a clean exit.
`--keep` retains it, and a failed run always keeps it for inspection.
`-w NAME` creates or reuses that exact workspace after checking its isolation and always retains it.

### `ride along <bro> [prompt]`

Starts an interactive session.
The default hold is `attended`, and the workspace is kept after exit.
`--unboxed` runs the workspace tree directly on the launcher's filesystem and changes the omitted hold to `guided`, because a non-guided unboxed session skips permission prompts without a container boundary.

A launch without `-w / --workspace` receives a fresh name.
`-w NAME` creates or reuses that exact workspace after checking its isolation;
a pinned workspace cannot be combined with `--drop`.
For an automatically named workspace, `--drop` removes it only after a clean exit and keeps a failed session for inspection.

Both mode verbs start **detached**:
`/tree` is an empty writable directory and no repository is cloned.
With `--repo PATH|URL`, the clone is `/tree/<repository name>/` and the session starts there;
an unboxed session starts in the same subdirectory of its host tree.
The tree beside the clone is writable session space, outside git.
`--repo PATH|URL` accepts either an existing path inside a checkout (resolved to its root) or a scheme/scp-shaped git URL.
A path attachment bases on the checkout's current `HEAD`;
a URL attachment fetches its managed mirror and bases on the fresh `origin/HEAD`.
`--into REF` selects another branch, tag, or commit, requires `--repo`, and affects only workspace creation.
Uncommitted host changes never transfer.

A session runs with what its bro declares and ride's own flags set:
neither mode verb forwards arguments to the harness program, on any harness, and a launch carrying a `--` tail fails before any workspace exists.

Shared launch flags are `--repo`, `--boxed`, `--unboxed`, `--hold`, `--cred`, `--grant`, `--revoke`, `--limit`, `--into`, `--no-trails`, `--env`, `--session-log`, and `--llm`.
`--cred KIND+INSTANCE` selects an instance and holds that kind.
`--grant`, `--revoke`, and `--limit` shape authority, while bare kinds in grants/revokes add or drop session credentials.
The [permission reference](permissions.md) defines the grammar, checks, and shipped worker options.
`--no-trails` disables trail recording for the session, whichever harness runs:
the launch omits recording's optional `trails` need and sets `TRAILS_DISABLED` for the run.
`--env NAME=VALUE` (repeatable) adds a variable to the session environment of the root and of every party member it summons:
it is the lowest layer of that environment, beneath the launch's own variables and the ambient inputs the launch admits, and the recorded session spec carries it, so a resume and every summon repeat it.
`--session-log LEVEL` sets the level the session's own processes log at
— `do-ride` and every framework process its harness starts read `BRO_LOG_LEVEL`
— by recording the `--env` addition `BRO_LOG_LEVEL=LEVEL`, so every party member and a resume carry it the same way;
the launcher's own `--log` never reaches the session.

### Lifecycle verbs

- `ride resume <workspace> [--llm RECIPE]` runs the recorded recipe and source unless `--llm` replaces them, with optional `--cred`, `--grant`, `--revoke`, and `--limit` adjustments.
  Resuming a solo run opens an interactive conversation in the same workspace, preserving its recorded hold.
  A resume checks the recorded recipe for admission and harness support before starting.
  `--llm` replaces it on the recorded harness, with source `--llm`;
  only the replacement is checked for admission, so a retired recorded model can resume with a listed model or with `+passthrough`.
  Records without a resolved-recipe record predate this format and are refused with recovery instructions, but remain visible in `ride list`.
  A record naming a materialized runtime bundle re-executes that bundle’s `ride` before reading the record.
- `ride override <mission> --grant N` / `--revoke N` changes a running session's bro targets (`@<bro>`) or credential kinds;
  `--cred kind+instance` changes a held instance and holds its kind.
  Requests go through the root's operator channel.
  `<mission>` is a mission id, as `ride missions <root>` lists it and a session's banner reports its own as `mission_id`;
  repeated flags are applied in order, with the last flag per name winning.
  The banner reads the updated authority, and the next summon uses it.
  An override affects no already-running child and a resume retains it.
  Credential replies name an operation and report `applied` on acknowledgement or `uncertain` without one.
  Pending operations block further overrides of that session, not its own starts;
  receipts settle uncertain operations while the applier is reachable, after the session ends, or at the workspace's next resume.
  The reply lists effects that wait for the next start, including tool selection and process-latched clients.
- `ride list` lists every workspace, its activity state, when its session last marked activity, its attachment, and its subject.
  Live badges are `[o]` boxed and `.o.` unboxed;
  idle badges are `[-]` boxed and `.-.` unboxed.
- `ride clean` removes inactive clean workspaces, managed URL mirrors no workspace references, unlocked runtime bundles, and each installed harness's unused host caches;
  `--force` permits dirty workspaces and removal when an attached repository no longer exists, while `--dry-run` reports only.
- `ride exec <workspace> [command ...]` enters a running boxed workspace in its start directory.
  The exec process is outside the running session's process tree and has no broker channel of its own.
- `ride check-clean <workspace>` reports whether removal is safe.
- `ride check-host [--install]` probes the host prerequisites a launch needs and prints each with the fix for its macOS or Ubuntu host, exiting nonzero while any fails;
  `--install` first runs the fixes that need no root.
  It refuses inside a managed session, which is not the host it would check.
- `ride scope [--bro BRO] [--harness HARNESS] [--llm RECIPE]` prints the prospective descriptor, base authority, credential picks, and name-presence states.
  `PRESENT` means the selected name has material or a typed-source entry, `SKIPPED` is an unpicked optional empty instance that is absent, and `MISSING` would fail the launch.
  Omitting `--bro` uses the installation's `default-bro`.
  It prints `llm: <canonical text> (<source>)`, or the refusal with a nonzero exit status.
- `ride banner [--plain]` renders the session facts, including `llm: <canonical text> (<source>)`;
  `--plain` selects plain `key: value` lines instead of the visual banner.

## Harness selection

`--harness` selects any name registered by this installation's `bro.harnesses` entry points.
A root selects `--harness`, otherwise its bro's stack's `harness`.
A summon selects its `harness` field, otherwise the child's stack's `summon-harness`, otherwise that stack's `harness`;
a `summon-harness` in any layer wins over the `harness` fallback.
A manual commit selects the operator's `--harness`, otherwise the child's stack's `harness`.
There is no framework default;
a missing choice refuses the start, naming the relevant keys and `--harness`.
A selected harness must be installed, and `ride resume` keeps its recorded harness.

A provider names the vendor and a harness the driving loop.
Each harness serves the providers its `llm_providers` names.
Recipes never switch harnesses;
an unsupported provider is refused with the installed harnesses that serve it.
`ride check-harness <name>` runs that harness's runtime probe.

### Recipe selection

The configured recipe is the last `llm.<harness>` entry in the bro's [home-layer stack](#home-settings), with its source layer.
Two layers never combine into one recipe, and a layer naming only `bro` leaves `claude` to earlier layers.
Personas declare no recipe.
`ride scope` prints the recipe and source or the refusal a launch would meet.
With no configured recipe or explicit recipe, a launch refuses before running, naming `--llm` and the configuration key.

Configuration recipes use `provider:model[+passthrough][,field=value]...`, with provider-native fields, such as `openai:gpt-6.1-sol,reasoning_effort=high`.
Models must be listed unless the recipe carries `+passthrough`.
An explicit `--llm` uses the same recipe text and replaces the configured recipe whole.
A summon's `llm` does the same, within the child's selected harness.
Every recipe names a model id; incomplete texts such as `:sol` and `openai` are refused.
There are no piece flags or preset tables.

### Harness seam

`bro.harness.Harness` is the framework identity, registered as one object per harness through `bro.harnesses`.
The registry reads installed names from metadata without importing their modules and lazily loads only the selected object.
`ride.harness.SessionHarness` is the managed-runtime boundary that object implements.
The neutral layer (`ride/ride/session.py`) owns one started-party launcher parameterized by workspace isolation.
It prepares either a container `Launch` or an explicit process launch, and a session harness supplies what differs:

- its `ScopeRecipe` and auth preflight;
- session preparation and the run under `do-ride` inside the prepared workspace, consumed by both isolations;
- a runtime probe for `ride check-harness`, host setup, runtime-image contributions, and relocatable-bundle assets;
- session existence with its resume-refusal wording and the subject read;
- the boxed extras (env, mounts) and the unboxed runner-env preparation.

`scope_recipe` takes no session, so surfaces with no session
— `ride scope`, dive-in's task prefetch
— resolve their recipe through the same seam.
The recipe carries the harness object into persona credential selection.
Each harness's recipe and implementation ship from a distribution of its own, which depends on `bro-ride`;
`bro-ride` discovers installed harnesses by entry point and depends on none.

What a harness runs inside a session, the state it keeps there, and how it records are the harness's own, documented with it in a man page of its own:
the `bro` harness's is `bro-harness` (`harnesses/native/harness/bro/native/reference.md`),
and the `claude` harness's is `claude-harness` (`harnesses/claude/harness/bro/claude/reference.md`).

## Operator channel

`ride missions <root>` lists a running root's mission tree as JSON, one row per mission.
Every row carries the same common fields:
the mission's `id`, its `parent` mission id (null for the root), its `type` and `state`, and its `workspace`, null for a mission that runs in no tree.
A joined bro member's `workspace` is its summoner's.
`options` holds the mission's admitted option values as its type declares them, current through every override;
it is null for a manual launch the operator has not committed yet.
`extension` is the type's own view of the launch, carrying what the options do not, or null for a type that keeps none:
a bro reports its effective `isolation` and, for a joined member, its `member` name;
a webview reports its `allowed_origins` and `blocked_origins`.
The command re-executes into the root's runtime before contacting its broker.
A root with no operator control file is refused by name.

A brokered root provisions a separate operator channel before starting its session.
Its address, including its secret token, and runtime reference live in a 0600 file under `<runtime-root>/control/`, outside every container mount.
The root removes the file when it ends;
`ride clean` sweeps files whose root was hard-killed, leaving live roots' files alone.
Operator requests are refused on every mission channel.
Like unboxed credential scoping, this is no security boundary against an unboxed session running as the operator's own user.

## Session watches

Every managed harness keeps its command watches in one store under the session state directory.
`do-ride` clears that store when a session starts, owns its producers for the session lifetime, and stops them in teardown.

A producer runs its shell command under the same supervisor as a job.
It is detached from the process that requested it, but exits when its session owner's liveness handle closes;
stopping it terminates the command and its descendants,
and waits for any it may not kill to exit on their own, naming each in the watch's lines.
It copies the command's output into the watch's log as it arrives, recording when each line's first byte arrived,
and publishes each journal head the command sends once the output the command wrote before it is in the log.
If the copy fails, the producer ends the command and its descendants.
A job carries the same backstop from the process that started it.

`take()` is the store's single reader.
Under an exclusive lock it commits every complete line past each watch's offset and returns them as one batch, each line tagged by command.
Each line of the batch also carries its arrival time and whether it wakes the session, for a harness to show its human;
the model reads the tagged lines alone.
A harness delivers each watch's lines of a batch separately:
kept whole in the session's results store (`bro::results` lists them as `bro::watch`),
and delivered as their head, up to one kept-result window.
Where they are cut, the message closes on the `bro::page` call that reads on.
A model pages, greps or ignores the rest, and a busy producer cannot bury another watch's lines.

A producer marks a line quiet by opening it with the ASCII unit separator (0x1F), which `take()` strips.
A quiet line never wakes the session on its own:
`take()` returns nothing while only quiet lines wait, and a batch that a waking line brings carries them in order, so every harness delivers them beside the next waking line.
Only waking lines hold a one-shot turn end open, and a run that ends drops the quiet lines still waiting.
A watch set to wake on quiet lines treats its every line as waking.
A producer marks the lines after the first of a multi-line event as continuations of the line before them with the ASCII synchronous idle byte (0x16), which `take()` strips;
`watches.continued` marks one and refuses a line already marked.
A continuation wakes the session exactly when the line it continues does, so it carries no quiet mark of its own,
and one with no line before it in its watch's batch wakes.
A harness that draws a batch for its human may show an event as one row.

Where a persona declares shell reach, every harness mounts `bro::watch(command, wake_on_quiet)` and `bro::unwatch(command)`.
Under a finite command list a watch runs its line in brash under the session's brash policy, and under `brash(ANY)` in bash;
`wake_on_quiet` sets the watch to wake on its quiet lines, while `unwatch` stops the watch it names without checking the command list and refuses the runtime-owned session watch.
`do-ride` arms `mission watch` before the harness starts for every session with a broker channel, so operator scope changes and artifact shares wake sessions that started without summon targets too.
`do-ride` restarts the session watch once a second while its producer is gone, keeping its log and the journal head it last published;
the dead producer's exit line reaches the session as usual, and the new one opens with a `mission watch resumed after …` line.
A restarted watch reads the journal on from that head, so no event is lost while the journal still retains it, and one the dead producer yielded but had not yet published may repeat;
past the retention window the stream reports a gap.
A read that times out reconnects from the stream's cursor, finishing any replay it interrupted, and reports a gap only if the next read finds the retention expired.
Each joined party member has its own session directory and therefore its own store.
The session watch reaches the model through the selected harness's delivery port without adding commands to the persona's tool reach.

A healthy wait costs no turn:
only failed work gets a turn-end notice, and background work earns one when it stays silent.
Background work is silent while it prints nothing and the model does not read it;
a silent piece of work notices after `PATIENCE_SECONDS`, then after waits that double up to `PATIENCE_CAP_SECONDS` counted from each notice, and any output or read starts the count over.
A watch producer may promise its next beat with `watches.beat(within_seconds)` (shell: `watch-beat <within_seconds>`), from the loop that does its work;
a producer that has beaten and then goes `BEAT_MISSES_ALLOWED` promises without another is stalled, which is a failed waiter, and a producer that never beats is judged by its process alone.
An idle run wakes for a stall as it falls due, and the stall is reported once per beat.

## Boxed launches and Docker daemons

`ride` can launch from a physical host or from inside another container.
When a bundle's container runtime needs materializing, it starts a throwaway runtime-image container and reads a nonce through a bind of the runtime root;
a marker beside the bundle then carries that verdict against the instance label the volume it was reached for was created with, so later launches against that same volume repeat neither the preflight nor the materialization.
Another daemon's volume of the same name, or a replacement created in its place, carries a different label and sends the launch back through both.
A daemon that cannot see the launcher's filesystem fails this preflight with its endpoint and the runtime-root path.
The nonce covers every boxed bind because each bind source must resolve under the runtime root;
a launch refuses an outside source rather than sending an unverifiable path to the daemon.
An unboxed launch needs no Docker daemon and works wherever `ride` itself runs.

## Runtime state

The runtime command is `ride`, and runtime-owned environment facts use `RIDE_*`.
Persistent runtime data lives outside every checkout under the user's global **`<runtime-root>`**:
`~/.local/share/ride/`, or `$XDG_DATA_HOME/ride/` where that variable names an absolute path.
A launch creates it with mode 0700 on first use.
Its top-level stores are `workspaces/`, `runtime/`, `repos/`, `trails/`, `launch/`, and `broker/`, beside the host caches installed harnesses keep there;
workspace metadata records which repository, if any, each workspace is attached to.

Workspace metadata is read only in its current strict shape.
A command naming a workspace whose record cannot be read fails with the read error and the `ride clean --force <name>` hint.
Enumerations mark it unrecognised instead of raising:
`ride list` lists it with that hint and logs the error details, and `ride clean` without workspace names leaves it.
Removing an unrecognised workspace requires its name and `--force`;
while any such workspace remains, cleanup keeps every managed mirror because its references cannot be read.
No earlier record format is recognised or migrated.
That explicit force-clean path removes the directory without interpreting its metadata, after the same session-lock and running-container checks used for a current workspace.

### Runtime bundles

Every outer root freezes the invoking installation's **bro toolset**.
Its roots are the installed distributions declaring an entry point in any group whose name starts with `bro.`, and the toolset is their dependency closure over counted requirements.
A requirement counts when its environment marker holds unconditionally or under one of its distribution's active extras.
An extra is active when the installation satisfies every requirement it adds, including the extras those requirements name;
mutually recursive extras are active together unless an unsatisfied requirement reaches their cycle.
A counted requirement the installation does not satisfy fails the freeze, naming the requirement and the member.
Distributions outside the toolset contribute nothing to the bundle or its hash.
Every toolset member must declare what it imports, since an undeclared dependency outside that closure is absent in sessions.

The freeze carries each member by its recorded provenance:
index installations become exact `name==version` pins;
version-control and remote-archive installations become direct-reference pins at their resolved commit or recorded hash;
local sources — a directory or an archive
— are carried in the bundle as wheels built or copied from their current contents.
A directory's wheel is built through its sdist, so a leftover in the tree, such as a stale setuptools `build/lib`, never reaches it.
When the directory is in a git checkout, the wheel's `.dist-info` also records the checkout's commit and whether the tree differed from it,
which is the commit `bro --version` names inside the bundle.
Their entries are laid out in a fixed order and carry one fixed timestamp rather than the build time a backend may record,
so an unchanged source tree at an unchanged commit keeps resolving to the bundle it already froze.
The Python major/minor and the installation's home join the manifest, and a content hash names the persisted bundle under `~/.local/share/ride/runtime/<hash>/` (or `$XDG_DATA_HOME/ride/runtime/<hash>/`).
Carrying local sources keeps the snapshot self-reproducing:
re-resolving from a materialized bundle's own venv lands on the same hash.
A toolset member that cannot be pinned reproducibly
— a vanished local source, a version-control installation with no resolved commit
— fails the launch naming what to reinstall it from, instead of silently escaping the snapshot.
The root holds the bundle's shared flock until its session and summoned children exit;
`ride clean` removes only bundles whose lock is available, each with its runtime volume,
and sweeps up what a hard-killed ride can leave on the daemon:
labeled materializer containers whose bundle is gone or reclaimable, and runtime volumes no bundle directory names.

Materialization writes the manifest's home into each environment's `bro-installation-home` marker, so every session and summon sees its root's home.
Every root materializes the bundle once as `host/venv`, checks its dependency closure, and builds `host/bin` as symlinks to console scripts declared through `bro.session_commands`,
then re-executes `host/venv/bin/ride` before reading workspace state,
so the launcher
— the broker root and the `launch` handler it hosts, whose imports and prompt reads resolve lazily
— runs the snapshot rather than the installation that invoked it.
The re-executed launcher freezes the venv it runs from, which lands on that same bundle;
a freeze from inside a frozen bundle that lands elsewhere fails the launch, since the launcher would otherwise re-execute without end.
The bundle's lock handle is inheritable, so the hold crosses the exec,
and the exec drops every `PYTHON*` variable from the environment, so an ambient `PYTHONPATH` or `PYTHONHOME` selects no code outside the bundle.
A change to a local source therefore reaches the next launch through a fresh freeze and never a running ride.
`--runtime-bundle PATH` instead takes an existing materialized layout at `PATH/venv` and `PATH/bin`.
The latter must be the exact shim farm for the former's full `bro.session_commands` roster;
a missing executable, missing or extra shim, or shim targeting another command fails before launch.
The launcher re-executes from `PATH/venv/bin/ride` the same way,
records the path for root and started-child resume, and uses its absolute `do-ride`,
so every process in the ride runs that runtime even when the first command came from another installation.
A given runtime also carries the roots its sessions trust:
an unboxed session of one gets `SSL_CERT_FILE` set to the certifi store inside `PATH/venv`,
so a runtime shipped to a host verifies TLS against what it ships rather than against a system store that host may lack;
a frozen runtime's sessions carry no trust setting and verify against the host's own store,
and neither admits a trust setting from the launcher's environment.
A given runtime has no frozen manifest from which to build a container volume;
a boxed root or boxed party start fails naming that constraint.
Boxed isolation from a frozen runtime uses the same materializer inside the runtime image to populate `ride-runtime-<hash>`, mounted read-only at `/var/ride/runtime`;
the volume holds `venv/` and the matching `bin/` shim farm.
The materializer container's lifetime is tethered to the launching ride
— its main command exits when the ride's death closes its stdin, and `--rm` then has the daemon remove it
— so a killed launch cannot strand a container pinning the volume.
Before each host or container materialization, the launcher stages every git pin under the bundle's `git-pins/` directory, holding the materialization lock.
Each distinct URL gets one bare repository, fetched at every pinned commit with its whole history and the tags in it, so builds deriving their version from tags keep that version.
The fetch uses the launcher's ambient git authentication with prompts disabled;
a failed fetch fails the materialization naming the pin and git's message.
A directory left by a killed launch is replaced, and staging is removed when materialization ends, on success or failure.

The install reaches these copies through `url.<staged repository>.insteadOf` entries supplied through `GIT_CONFIG_COUNT`.
The host install reads no system or global git configuration and inherits no environment-carried git configuration, so an existing URL rewrite cannot divert it from a staged copy.
The container materializer reaches them at `/bundle/git-pins/` with no launcher credentials;
before installing as root, it names each staged repository in its own system git configuration's `safe.directory` so git can serve the launching user's repositories.
The installed records keep the original URLs and commits, and staging changes neither the manifest nor its hash.
Session containers, worker containers and foreground worker runs use this same materialization.

Staging covers a git pin's own commit and history, not its submodules, build hooks that fetch additional git sources, private package indexes or authenticated remote archives.
The same limits apply to host and container installs:
anything beyond the staged copies must be reachable without the launcher's git configuration, or installation fails with the installer's error naming the distribution.
A git server speaking only the original protocol must permit fetching by commit id, or staging fails for either materialization with git's error.
The session PATH starts with the runtime shims and then the system paths in both isolations, with the launcher's active venv removed on the launcher.
Repository commands are explicit through `uv run` or `.venv/bin/`.
The shims serve the session;
the machinery a session spawns for itself, a harness's session daemons among it, is named by its path in the runtime the spawning process runs from and never looked up on that PATH,
so the roster is what a session may run rather than what it needs to work.

### Managed URL mirrors

A URL attachment is normalized and mapped to `<runtime-root>/repos/<slug>-<digest>/`.
The directory is a bare repository with `origin` set to the attachment URL and `gc.auto=0`.
Every launch naming the URL takes the mirror's flock, fetches without pruning, refreshes `origin/HEAD`, and resolves that commit as the default base.
Every attached workspace copies or hardlinks the mirror's objects into its own independent clone on first launch.
`ride clean` removes the whole mirror only after no workspace metadata references its URL.
The launcher's ambient git authentication performs mirror fetches and first-launch submodule initialization, while the scoped in-session credential hook handles later operations from the workspace clone.

## Workspaces

A workspace is one directory, `<runtime-root>/workspaces/<name>/`, holding its writable tree plus everything the launch machinery records about it.
Workspace names are global, so every lifecycle verb identifies one by name from any directory.
A name is one path component, and one a launch cannot use fails naming it rather than reaching the filesystem.
A directory with a usable workspace name but no readable record is enumerated as unrecognised ("Runtime state"):

```
<runtime-root>/workspaces/<name>/
  workspace.json      what the workspace is (below)
  tree/               writable session directory, /tree in a container
    <attachment name>/  host-provided attachment, such as a repo clone
  lock                the session lock ("One session per workspace")
  exit                how the last session ended (below)
  resume.json         the spec `ride resume` relaunches ("Lifecycle verbs")
  session.log         the launching process's mid-session output ("The broker channel")
  session/            what the running session records about itself:
    current-trail.json            the trail the session records into ("Summoning another bro")
    session-recorder-health.json  the recording health signal ("Session recording")
    watch/                        the session's watch declarations, offsets, and output
    results/                      the session's kept tool results (`bro/AGENTS.md`, "Kept results")
    <harness>/                    the harness's own session artifacts
  <harness>/          a harness's own state dir, where it keeps one
  party/<member>/     records for a session that joined this workspace's party:
    session/          the member's session records, with the same shape as above
    <harness>/        the member's private harness state
    trails/           a local-recording boxed member's trails, staged here then adopted into the ride's host store on exit
```

A session starts in its attachment directory, or in the tree when detached.
A joined member and `ride exec` use their party's start directory.
Harness state keyed by directory, including Claude's project and trusted path, uses that start directory.
The tree sits in its own subdirectory rather than being the workspace directory itself:
a container bind-mounts it as `/tree`, and the records must stay outside that mount.
Whichever harness ran it, everything a session leaves behind is one of these records, so removing the workspace directory is what reclaims it.
Of the last three, `session/` is the unconditional one
— every session records into it, whichever harness runs it.
It is the only record the session reaches from *inside* itself:
unboxed isolation uses its absolute path, and boxed isolation uses a bind at `/var/ride/session`, either way named by `RIDE_SESSION_DIR`.
That reach is what it exists for:
the trail pointer and the recording health signal are published by the session and read back host-side, while the session's producer and reader processes share `watch/`.
Signals every harness shares sit at its root, a harness's own artifacts under `<harness>/`.
Every unboxed session puts its scoped store and install-hook output under one private temporary root removed when the session ends, so retained or collected records hold no secret.
A box keeps their equivalents in its own layer.
A harness's state dir belongs to that harness in both isolations.
A joined member keeps the same record shape under `party/<member>/` while sharing the workspace tree.
A boxed workspace bind-mounts the whole `party/` root at `/var/ride/party` when its container is created, so a member's dirs, created later at join time, are reachable inside without a mount of their own;
an unboxed member reaches them by absolute host path.
A boxed member whose own scope records to the local trails backend stages its trails under `party/<member>/trails` rather than the first session's `/var/ride/trails` bind, which its scope may not have (`RIDE_TRAILS_ROOT` names it);
its supervisor adopts them into the ride's own local trails store when the member settles, so the trail survives its records and is discoverable there like any other.
An unboxed member records straight to the ride's local trails store like any host session.
Its records are removed after a clean exit and kept after failure or kill;
the member is not resumable, so its trail is the recovery record.
The one deliberate exception to all of this is the launch audit, under `<runtime-root>/launch/`, because it must survive a workspace drop.

`workspace.json` is written once at creation and read by every later launch, so nothing downstream re-derives it:

```json
{"isolation": "boxed", "attachments": [{"type": "repo", "name": "project", "repo": "/home/me/project", "branch": "workspace-my-task"}], "throwaway": false, "tree": null}
```

- **`isolation`** — `boxed` (the default) or `unboxed` (`--unboxed`).
  It is fixed at creation;
  a launch naming an existing workspace must request the same isolation.
- **`attachments`** — a non-empty list, present exactly when the workspace has an attachment.
  A launch supports at most one attachment.
  Each record holds its registered `type`, its directory `name`, and the fields its type owns.
  A `repo` record holds `repo`, the resolved checkout path or normalized git URL, and `branch`, the clone's branch (`workspace-<name>` for a new workspace).
  Reusing a workspace name with a different attachment type or identity is refused.
- **`throwaway`** — the workspace is disposable.
  Its supervisor removes it once its session exits cleanly.
  It is set for the workspaces summoned children run in.
- **`tree`** — the absolute external tree named by `--tree`, otherwise `null`.
  An external tree belongs only to a detached unboxed workspace, must exist outside the runtime root, and may be recorded by one workspace at a time.
  That ownership check inspects the records it can read and passes over unrecognised records.
  Resume fails if it disappears;
  workspace removal deletes only the records and never this directory.

A detached record is `{"isolation": "boxed", "throwaway": false, "tree": null}`.

`exit` records how the workspace's last session ended.
The session's supervisor clears it when the session starts and, once the session ends, writes the exit code there, or `killed` for a child the host killed;
a session that dies before its supervisor writes, such as one under a crashed launcher, leaves no record.
A managed detached tree is clean exactly when it is empty;
a workspace with a `repo` attachment or an external tree is clean only when `exit` records `0`,
so a failed, killed, or unrecorded session keeps it for inspection and recovery.
Other attachment types supply their own cleanliness rules.
Removing a `repo` workspace whose recorded checkout or managed mirror no longer exists requires `ride clean --force`.

## The launch stack

Every managed session launches through the same stack, whichever harness drives it;
`--unboxed` changes only the outer machinery:

- **the neutral outer** (`ride/ride/session.py:start_session` and `started_party_launch`)
  — policy validation, one isolation-parameterized workspace preparation path, session supervision, and post-exit UX.
  Roots and spawned children use the same started-party launcher;
  it touches the selected harness only through the seam (see "Harness seam").
  See "The outer layer".
- **the session executable** (`do-ride solo|along` → `ride/ride/do_ride.py`), spawned by the outer in the prepared workspace.
  Unboxed isolation invokes the frozen snapshot's absolute `do-ride`;
  boxed isolation resolves the same pinned command from `/var/ride/runtime/bin`.
  One code path for every flag combination and every harness carries the session environment and persona provisioning, then hands off to the selected harness's run.
  See "The session executable".
- **the harness's session** — what the selected harness runs in the prepared workspace, themed with the session's bro, confined to the harness plus its `ScopeRecipe` (the secret manifest), which the outer consumes through the seam.
  Each harness's own man page describes it (see "Harness seam").

A neutral session-shaping flag lands once in the outer and reaches both execution modes and every harness;
a harness-shaping one lands once in that harness and applies to both isolations by construction.

## Session permissions and credentials

A managed session's host checks starts against use/give authority and delivers admitted credential picks through a synthesized store.
The [permission reference](permissions.md) owns the model mapping, grammar, configuration layers, type options, starts, overrides, resumes, records, and refusals.
The [cheat sheet](permissions_cheatsheet.md) gives everyday setups; the [model](permission_model.md) defines the abstract rules.
This section covers storage, hydration, and delivery of credential material.

A credential store has this shape:

```text
<store>/creds/<kind>[+<instance>].cred
<store>/creds.json
```

```json
{
  "defaults": ["github+dev", "trails+write"],
  "sources": {
    "github+reviewer": {"type": "github_app"},
    "openai": {"type": "ssm", "parameter": "/bro/openai", "region": "eu-west-1"}
  }
}
```

`defaults` uses the `creds` list grammar, selects at most one instance per kind, and is the store's own base for kind-addressed reads.
`sources` annotates stored names with typed source configuration;
annotations for kinds this installation does not register are skipped after their source shape is validated.
Both keys are optional, and any credential annotation outside `sources` fails with the migration to the current shape.
A caller's picks layer over the store's `defaults`, then an unpicked kind reads its empty instance.
`BRO_STORE` directs a process to that store alone and bypasses `~/.bro.json`.

For host commands, `user.creds` and then `user.tools.<command>.creds` layer over the host store's defaults.
Store defaults and host-command picks for unregistered kinds are skipped with a verbose diagnostic after syntax validation, so one host configuration can serve installations with different plug-ins.
A read of an unregistered kind remains unavailable even if its material exists.
Explicit caller selections still require registered kinds.
For managed launches, credential selection follows the root reading and the admitted descriptor described in [permissions](permissions.md#bro).
The hydration step uses those exact picks, not a fresh read of host configuration.
`ride scope` reports the prospective values and their presence.

A credential kind may declare its material literal in the code registry.
The resolver returns that kind's material without expanding any `$cred` reference node in it, and scoped-store hydration follows none of those nodes.

The session store materializes every admitted instance under its own stored name, preserving an instance-spelled `$cred` target as that instance.
Its `creds.json` records the launch's pick in `defaults` for every loaded kind and every kind reached by a kind-spelled preserved reference, and copies the typed source annotations under `sources`.
The session index also carries `generation`, `kinds`, and `material`, mapping each readable stored name to an immutable file under `creds/`.
Files outside that mapping are unreadable through the resolver.
Readers refresh their selection and cached material when the generation advances;
explicit-instance reads never substitute the current pick.
An optional kind is skipped only when no layer picked it and its empty instance is absent;
every required or explicitly picked name must be present and load successfully before launch.
A missing source, minting failure, malformed value, or unresolved reference fails the launch under that stored name.

`credentials.build_scoped_store` also returns the declared kinds that loaded.
Both isolations publish those through `BRO_INSTALL_KINDS` beside `BRO_STORE`, and `do-ride` runs install hooks for those kinds only, each reading the session store's default instance.
Kinds pulled in only through `$cred` references receive no install hook of their own.
The hook directory is recreated for each launch, and two hooks claiming the same file, variable, or command fail.
A credential override stages changed kinds without touching other kinds, then replaces the index by rename as its commit point.
It replaces the changed hook files, wrappers and environment file, removes material the index no longer names, and writes a host-side receipt after completion.
Publication after the commit point is idempotent and retried;
completed operation ids prevent stale retries from undoing later changes.
The hooks' `bin` directory is on PATH even when empty.
Each harness applies the current environment file to the commands its model runs, and framework watch producers and CLI tools apply it as an overlay.
A running command keeps material it already opened;
new commands and store reads use the published state.
Workspace retention and receipt settlement are described in [credential overrides](permissions.md#credential-changes-and-settlement).
A boxed session receives the store in its writable container layer at `/home/ride/.bro`;
an unboxed session and its hook output live under one private temporary root removed by its supervisor.
Unboxed scoping remains a convenience rather than a security boundary because the process runs as the host user.

The benchmark adapter is a separate store writer and reader boundary.
The trial bundle's own framework reads the host store with its `defaults`, hydrates the one trial credential into a store of the same shape, and uploads that store beside the same bundle that later reads it.
The bundle therefore owns both sides of the trial-store format rather than relying on the benchmark launcher's installed framework version.


## Home settings

The installation names its home in its environment's `bro-installation-home` marker.
The home ships launch settings as a `bro-settings.toml` package resource through `bro.settings` ([extending](extending.md#home-settings)).
These settings apply regardless of the repository the ride attaches:

```toml
default-bro = "foo"
harness = "claude"
summon-harness = "bro"
summon-depth = 4
grant = ["github", "@reviewer", "bro.placement=join"]
revoke = ["bro.placement=boxed"]
```

`default-bro` names the bro utilities such as `dive-in` and `ride scope` use when none is supplied;
the mode verbs still require their bro positional.
`harness` and `summon-harness` supply the selections described in "Harness selection".
Each may be omitted when the launch or host configuration supplies it.
`summon-depth` is a non-negative integer controlling the [summon allowance](#summon-allowance).
Home settings may name credential kinds and authority entries, but not host credential instances.

For bro B of home H in installation I, the stack is:

1. I's packaged home settings;
2. host `defaults`;
3. `homes.I`;
4. `homes.H.bros.B`, when H differs from I;
5. `homes.I.bros.B`.

`default-bro`, `harness` and `summon-harness` take the last setting.
For `summon-depth`, the general layers set the root allowance and the bro layers set its cap, each last-wins.
`llm` takes the last value per harness;
`creds` takes the last pick per kind over the store defaults.
Authority folds caps and then grants in stack order.
An absent layer contributes nothing.
The child bro's own stack governs its summon, not its summoner's stack.
The [host schema](../setup/AGENTS.md#host-config-brojson) defines each layer's allowed keys.

An installation validates its own home settings, `defaults` and `homes.I` in full at root start, every bro entry included.
Unknown keys, types, options, bro targets and credential kinds refuse every launch, naming the layer and key.
Recipes under installed harnesses bind fully;
recipes under absent harnesses are grammar-checked only.
Only installed foreign bros' own entries are read from other homes, and initially only their shape is checked.
A foreign recipe, harness or credential pick binds only when it wins;
foreign authority binds when its bro starts.
An unsupported value refuses that start with its source, the plug-in to install, and `homes.I.bros.B` as the place to override it.
Grants fold instead of shadowing, so answer an unwanted foreign grant with a revoke.
Unrelated home sections are never read.
A root freezes this reading for all its descendants and manual commits;
configuration edits affect later roots only.

### Attachments

An attachment is typed host-provided data in its own named subdirectory of the tree.
Types register through `bro.attachment_types` as `name = "module:object"`, loading an instance of `ride.attachments.AttachmentType` lazily by name.
The type validates its recorded fields and supplies resolution, preparation, session environment, a session-start step, child inheritance, cleanliness, removal, listing and shared-data cleanup.
The launcher passes each attachment's complete record to `do-ride`, since boxed sessions do not mount `workspace.json`.
The neutral launcher, workspace, container machinery and worker lowering use only that contract.

The `repo` type ships in `bro-ride` and is the only launch syntax:
`--repo PATH|URL` attaches a repository, while omitting it starts detached.
Its attachment name is the checkout path's last component, or the normalized URL's last path segment without a trailing `.git`.
An empty name, `.` or `..` refuses the launch.
The name is derived at creation and recorded, not re-derived at each start.

#### Repository configuration

Only `--repo PATH|URL` attaches a repository to `ride solo|along`; cwd is never an implicit attachment.
The clone's root may carry `.bro/config.toml`:

```toml
provision-command = "./setup.sh"

[devoops]
target-registry = "acme.targets:registry"
```

`provision-command` is an optional non-empty string.
Every other top-level entry must be an owner table, carried verbatim and interpreted by its owner;
`bro-oops`, for example, reads `[devoops]` from the repository it runs in.
An unknown top-level value refuses the file, naming the file and key.
A missing file means no repository configuration.
Repository configuration changes no launch policy:
that reading depends only on the installation and host configuration, not its attachment.
Credential picks live in host home and bro entries;
`--cred` overrides the pick for one launch.

At every attached session start, including resumes and started or manual children in either isolation,
`do-ride` reads the clone's current configuration and runs its declared command after the scoped credential hooks and before the persona's provisioning steps.
A detached session or joined member runs neither repository nor persona provisioning.
The command runs as `sh -c <provision-command>` in the clone's root, with the session environment overlaid by the credential hooks, including their PATH-front tools.
Its stdin is empty and both output streams go to `do-ride`'s stderr, outside a one-shot reply.
A nonzero status logs `provisioning command failed with exit status <n>: <command>` and ends the start with status 1 before the harness starts;
a refused configuration ends it with the reader's message and the same status.
The command has no time limit of its own, but a child's summon timeout includes it.
Because it runs on every start, its idempotency is the repository's contract.
A repository without a declaration sets itself up as its own instructions say, such as its `AGENTS.md` or a uv project's first `uv run`.

## The outer layer

Boxed isolation is the default, and `--boxed` spells it explicitly.
`--unboxed` runs the same clone directly on the launcher's filesystem.
Whatever the isolation and harness, the outer:

- validates policy once — the harness's auth precondition (`preflight_auth`) is a launch preflight, so `ride resume` is gated like the launch that created the session.
  Neither runs in `do-ride`, whose parser has no outer machinery flags and therefore no placement policy to revalidate;
- resolves and flock-holds one runtime bundle for the root's full lifetime
  — a freeze of the invoking installation, materialized for the host, or the materialized layout named by `--runtime-bundle`
  — and re-executes from its `ride`, so the rest of the launch and the broker root run that runtime;
  then sets `RIDE_COMMAND` (including the resolved `--repo` when attached) and resolves the session's base to a sha
  — `--into` against that attachment, or the attachment's HEAD when none was given;
- runs every precondition that can reject the launch
  — the harness's auth preflight and the credential/summon scope preflight
  — before the workspace is recorded, so a refused launch creates no workspace (a URL resolution may already have refreshed its reusable mirror);
- resolves the workspace, creating it with the selected isolation, attachment, and optional external tree when absent and refusing a mismatch or a tree another workspace records (see "Workspaces");
- takes the workspace's session lock, before either isolation prepares anything and for the session's whole duration (see "One session per workspace"), and records the spec `ride resume` would relaunch with;
- on a resume, fails fast when the workspace has no session to continue
  — the harness's cheap existence check with its own refusal wording (`session_exists` / `missing_session_error`), run before the tree is materialized for a mistyped name;
- calls the shared started-party launcher to prepare the workspace (the two isolation sections below) and the `do-ride` command with only the session shape;
  the launcher emits a container description for boxed isolation or a closed process-environment snapshot for unboxed isolation,
  whose `PWD` names the start directory and whose only ambient inputs are the baseline roster in `ride.runtime_bundle` and, for a session attached to the launcher's terminal, its terminal identity;
  both run the frozen bundle through the host materialization or container volume;
- owns the post-exit UX, identical in both isolations
  — the resume hint, `--drop` removal (honored only on a clean exit; see the flag).

#### One session per workspace

A second concurrent session on one workspace would mutate the same files and share the gitignored token-accounting state, so a launch holds an exclusive `flock` on the workspace's `lock` file
— taken atomically against a racing launcher, released even when the holder dies without unwinding, and covering the whole launch rather than a window inside it.
A refused launch names the holding pid.
The broker transfers the same lock into a started unboxed child's process handle until that child settles.
`ride list` and `ride clean` read the same lock as their liveness signal;
a boxed workspace additionally counts a running container bound to its mount, which is what a launcher killed outright leaves behind.
The lock releases with the session, so re-entry and `ride resume` afterwards are unaffected.

### Unboxed isolation (`ride along --unboxed -w <name> <bro>`)

An unboxed workspace runs its tree directly on the launcher's filesystem.
With a `repo` attachment, the first launch creates the same independent clone a boxed workspace uses and checks out the workspace's recorded branch at the resolved base.
Later launches preserve that clone exactly as the session left it.
After clone preparation, `ride` starts the runtime bundle's absolute `do-ride` with the start directory as cwd.
Every unboxed session's store and install-hook output live under one private temporary root that its supervisor removes after exit, so a retained workspace contains no credential material.
`BRO_STORE` and `BRO_INSTALL_DIR` point at those directories.
The harness's `prepare_unboxed_env` hook supplies launch-time state;
`do-ride` owns credential hooks and provisioning as described in "Repository configuration".
The launch removes the invoking environment's active-venv marker and PATH entry, so the start directory's `.venv` is never activated implicitly.

A detached unboxed launch normally creates a plain empty `tree/`, skips repository and persona provisioning, and runs the same pinned `do-ride` there.
With `--tree PATH`, it runs instead in that existing directory while keeping every record under the runtime root.
The auth and scoped-credential preflights happen before a workspace or clone is created.
On a clean dropped exit, the workspace records are removed directly;
an external tree is never removed.

### Boxed isolation (`ride along --boxed -w <name> <bro>` — the default)

`/tree` is always the workspace's writable `tree/` bind.
It starts and stays empty for a detached launch.
With a `repo` attachment, the launcher creates a **plain local clone** before container creation.
The same clone machinery serves both isolations and leaves no dependency on the attached checkout's git directory.
A local clone hardlinks objects when the attachment and runtime root share a filesystem and copies them otherwise;
it carries no alternates dependency on the attachment.
The attachment itself is not mounted into the container.
Layout:

- `<runtime-root>/workspaces/<name>/tree/` (host) → `/tree` rw.
  Empty while detached;
  an attached workspace contains the host-prepared clone at `<attachment name>/`.
- `ride-runtime-<bundle-hash>` → `/var/ride/runtime` ro.
  Its `bin/` is first on PATH and its `venv/` carries the root's frozen Python installation.
- a per-launch **scoped credential store** injected into `/home/ride/.bro`.
  Before the container starts, the host resolves only the secrets the session actually uses into an **in-memory** tar and `docker cp`s it into the created-but-unstarted container
  — there is no host-side store and no bind mount.
  Its authoritative `creds.json` maps readable stored names to material files and records the picks, source annotations and generation.
  The code registry stays in the frozen runtime bundle, and the index bounds the in-container resolver to the scoped set
  — any other secret resolves to a clean `SecretNotFound`.
  Hydration is **strict** — a missing secret raises on the launcher before the container is created.
  Living in the container's own writable layer, the store dies with the container:
  `--rm` removes it on normal exit, and an orphaned container (a killed `ride`) is reclaimed by `ride clean`'s container GC
  — temporary override payloads on the host are private to the supervisor and removed with their operation or root lifetime.
  See "Session permissions and credentials" above.
- **github** and **aws** are ordinary scoped secrets
  — no out-of-band `/run/secrets/github_token` mount, no `~/.aws` mount.
  Each carries a static **install hook** in the registry, applied generically by `credentials install-hooks` (see "Session permissions and credentials"):
  `github` → the git configuration the session carries in its own environment
  — a credential helper for `https://github.com` over a reset of whatever helper a config outside the session declares there, plus the rewrite that carries both ssh spellings of a github remote to it
  — and a PATH-front `gh` wrapper,
  each resolving the token via `credentials get` per operation (a `github_app`-backed instance mints short-lived tokens, so consumers read at use time rather than off a value baked in at install,
  and `GH_TOKEN` / `GITHUB_TOKEN` inherited from the launching shell are blanked so nothing in the session acts on another identity);
  `aws` → the shared-credentials file it points the CLI at.
  No per-secret logic lives in the entrypoint.
  The github token therefore authenticates every github remote and nothing else;
  in an unboxed session any other host keeps whatever the user's own configuration supplies.
  Two user settings still outrank it in an unboxed session, since git gives the session no way to override them:
  a `~/.netrc` entry for github.com, which curl uses ahead of any git credential helper,
  and the user's own `insteadOf` rule carrying github https remotes to ssh, which git applies before a session rule for the same prefix.
- the host's `/var/run/docker.sock` is **never** mounted:
  its API is root on the launcher with no per-caller authorization, so a socket grant would step past every scoped boundary above.
  Work that needs a daemon
  — building and pushing the operated project's images, say
  — goes through the project's CI instead.
- `<runtime-root>/workspaces/<name>/party/` (host) → `/var/ride/party` rw.
  The root under which joined members' records are created later ("Workspaces"), mounted whole at creation so a join needs no mount of its own.
- `<runtime-root>/trails` (host) → `/var/ride/trails` rw when the scoped trails backend is local.
  In-container readers resolve that fixed absolute path;
  no state path is relative to `/tree`.

On an attached workspace's first launch, the `repo` type clones from the checkout or managed mirror into a temporary directory beside the tree, then atomically publishes the completed clone at `tree/<attachment name>/`.
An existing attachment directory without a `.git` directory refuses the launch.
It retargets `origin` to the attachment's upstream URL, spelling a github.com one as `https://github.com/<owner>/<name>` and keeping any other host's verbatim, and ref-refreshes the attachment's `refs/remotes/origin/*` without adding another remote.
It creates the recorded workspace branch from the resolved base:
a URL attachment's fresh `origin/HEAD`, an explicit `--into`, or a summon's inherited base;
a path attachment defaults to its current `HEAD`.
Initialized checkout submodules are cloned from their matching host paths and retargeted to their upstreams, spelled the same way;
a managed bare mirror initializes them from their committed URLs, while an uninitialized checkout submodule is skipped.
No prepared clone or submodule carries an alternates file.
Later launches preserve the clone exactly as the session left it.

Inside the container, the entrypoint (running as root first):

1. Aligns the `ride` user's UID/GID with whoever owns `/tree` on the launcher,
   then re-execs as `ride` (skipped on Docker for Mac when the bind mount reports root-owned via virtiofs — remapping to UID 0 would make claude refuse `--dangerously-skip-permissions`).
2. Marks every directory safe for git with `safe.directory=*`.
   The host's `~/.gitconfig` is not seeded:
   a session's git configuration is what its own hooks declare later in `do-ride`.
3. Execs `do-ride`.
   Provisioning, credential hooks and the optional session broxy are per-session work owned by that executable.
   The workspace venv is not activated:
   PATH remains the pinned runtime shim farm plus system paths.

Every root and spawned child goes through `ride.session.started_party_launch` with a `SessionSpec`, workspace, hydrated scope, runtime bundle, and requested isolation.
The boxed result is one broker-free `ride.workspace.docker.Launch` carrying the full launch:
the workspace name, typed attachment records and their resolved base, command, explicit env snapshot, credential tiers, TTY, extra mounts, resolved image tag, and runtime bundle hash.
`prepare_container` consumes that immutable description for attachment preparation → scoped-store build → `docker create` + store copy;
it does not re-resolve images or bundles.
The unboxed result is a `ProcessLaunch` carrying the absolute snapshot command, cwd, and complete environment;
broker supervision adapts it to `ProcessLaunchSpec` without reading ambient process state.
An unboxed root defers image and volume resolution until its first boxed summon.

Every boxed session runs the runtime image (`bro/ride-runtime:<hash>`):
Python-minor-matched Debian, system CLIs, the ride user, entrypoint, shell helpers, and each installed harness's runtime contributions.
Its hash covers the Python minor, installed harness roster, Docker instructions, contributed files and build arguments.
It contains no Python distribution from the ride installation.
`ride.workspace.build_context` streams a normalized context containing only runtime assets to `docker build -`.

Provisioning builds the project's own environment in the clone, where it persists across later sessions and resumes.
A new workspace builds its own environment.
The uv cache stays in the container's own layer and is never shared between boxes;
the image sets `UV_LINK_MODE=copy` because that cache and the clone are on different filesystems.
Private project dependencies are installed under the session's own identity, so a session without access cannot install them.

Each process holds a host-wide shared file lock on every runtime tag it resolves until that process exits.
A missing tag is built under a host-wide per-tag build lock.
Superseded runtime tags are pruned per image repository only when a non-blocking exclusive lock proves that no current process holds the tag.
The removal keeps that lock until `docker image rm` finishes.
A process reaching for a tag during its removal waits and rebuilds it when the removal succeeds.
Cleanup of root-owned workspace files uses a present runtime image.

Network is not restricted by design.

When a boxed session exits, the workspace directory stays on disk for the next session, unless `--drop` was passed and the session exited cleanly (in which case `<runtime-root>/workspaces/<name>` — the tree and every record with it — is removed).

#### Ctrl+Z: suspend and resume

Inside the container no job-control shell sits above the session (`docker-init` is the session leader), so a raw Ctrl+Z byte reaching the container pty would stop the session's foreground group with nothing able to ever resume it
— a wedged terminal.
Every interactive attach therefore binds Ctrl+Z as the docker client's detach key (`--detach-keys=ctrl-z`, replacing the default `ctrl-p,ctrl-q` sequence — so Ctrl-P passes through to the session):
the byte never enters the container, and pressing Ctrl+Z detaches the host-side client instead.

ride tells a detach from a container exit by the container's running state
— the client exits 0 either way.
On detach it freezes the whole container (`docker pause`, the cgroup freezer) and stops its own process group, so the launching shell reports the job stopped exactly like an unboxed Ctrl+Z;
`fg` resumes ride, which thaws the container and re-attaches.
The entire session — the harness's program and every daemon it runs
— is frozen while suspended.
Both attach paths behave this way (the broker-supervised root and the broker-less fallback);
unboxed isolation needs none of it
— a real shell with job control sits above the session there, so plain job-control suspend already works.
With no job-control shell above ride itself (an orphaned process group), the kernel discards the self-stop and Ctrl+Z degrades to a brief pause + re-attach instead of a wedge.

### The broker channel

Every session runs as the root peer of a **broker** (see `bro/broker/AGENTS.md`):
the outer provisions a channel on the session's one listening port, points `BROKER_UPSTREAM` at `tcp://<token>@<host>:<port>`, and supervises the session from the broker's event loop until it exits.
The token is the channel's whole credential
— a port is reachable by every local process, so a connection is attributed to the channel whose token it opens with.
The listener binds loopback plus, when the docker daemon runs on this host, the bridge gateway a container reaches back through (`ride/ride/broker_root.py:broker_bind_hosts`);
a daemon in a VM names a gateway that is no address here, so only loopback binds and the VM's own `host.docker.internal` proxy carries the container to it.
The shared root supervisor adapts the started-party launch to the isolation's spawner (`ride/ride/workspace/spawn.py`, composed by `ride/ride/broker_root.py:run_root_via_broker`) and address host:

- boxed — `DockerLaunchSpec` through `DockerSpawner`, with an address naming `host.docker.internal`, which every launch maps to the host gateway with `--add-host`;
- unboxed — `ProcessLaunchSpec` through `ProcessSpawner`, with an address naming loopback.

The session's processes don't talk to that upstream directly:
a **broxy** (the peer-side broker proxy, `bro/broker/broxy.py`) consumes `BROKER_UPSTREAM`, holds its one long-lived connection, and publishes its own loopback address as `BROKER_CHANNEL`.
The session's short-lived clients (`broker` CLI calls, `RunLifecycle`, a backgrounded wait) therefore multiplex over the single connection the launcher's supersede-on-accept semantics expect.
Client recovery reads the host journal through `query`;
the broxy retains no result state.
`BROKER_CHANNEL` has one meaning everywhere:
it is the address clients connect to, whether a broxy supplied it or a launcher explicitly chose a direct no-proxy topology.

The lifecycle is one shared `broxy launch` sequence:
it starts `serve` detached with output redirected to the requested log, reads back the ephemeral address `serve` publishes, gates on readiness, kills a failed serve, and prints the local address plus pid.
There is no restart supervision:
the upstream is the session's own host broker, which never comes back within a session, so a broxy that dies takes the session's channel with it
— loudly, as a code bug to surface (`bro/broker/AGENTS.md` owns the policy).
`do-ride` owns that sequence in both isolations through `bro/launch/broxy.py:session_broxy` whenever `BROKER_UPSTREAM` is set and `BROKER_CHANNEL` is not.
It retains the returned pid to stop the daemon on session exit and writes `broxy.log` in the session directory.
When `broxy launch` cannot run
— missing from the session runtime or not ready within the gate
— the call site leaves `BROKER_UPSTREAM` set and `BROKER_CHANNEL` unset.
The session launch still proceeds, but every attempted broker client raises `session proxy failed at launch` instead of silently behaving like a broker-less session;
the launch warning names the log path.
With both variables unset, no channel was intended:
the substrate CLI stays inert, while summon and artifact surfaces report that no broker channel exists.
A proxy-less *summoned child* cannot report its result
— its exit surfaces to the summoner as the synthesized `result{failed, reason: exit}` with the output tail, and the child's trail as the fallback record.

The live broker registers the reserved `ping` kind, so a session can verify its channel with `broker request ping '{}'`;
the journal projection logs the root's host-anchored mission
— its launch carries the mission id in `BROKER_MISSION` beside the channel, and the host process is the owner;
and the `launch` kind handler over the installed `bro.worker_types` registry, including the bro, benchmark, and webview types.
The root provisions its mission record and registers the operator channel.
`LaunchControl` checks the [permission rules](permissions.md) before dispatch; worker types declare options and run admitted work, without making their own permission checks.
Because the channel sits on the critical path of every launch, a broker defect would too
— `BROKER_DISABLED` (presence-checked, parallel to `TRAILS_DISABLED`) is the kill-switch that skips broker provisioning and dispatch entirely, so no launcher starts a broxy.
The broker-less path runs `docker start -a -i` in boxed isolation and a plain runner spawn in unboxed isolation.
The post-exit finish (resume hint, `--drop`) runs after `Broker.run()` returns, so it is identical on both paths.

While an interactive root owns the terminal
— from the attached child's spawn to its exit
— the outer process's own output moves to the workspace's `session.log` (`ride/ride/workspace/spawn.py:_HostLogRedirect`, an fd-level redirect of the host's stdout+stderr):
summon lifecycle lines, broker warnings, and the inherited-fd chatter of spawner shell-outs (a mid-session `docker build` for a spawned child) land there instead of painting over the session's raw-mode TUI.
The flip is TTY-gated, so a headless run keeps everything on stderr;
launch-time output (scoped-secrets lines, a first image build) and the post-exit finish print before and after the attached span, so they stay on the terminal.
When anything was written during the span, one post-exit line points at it (`session host log: <path> (<n> lines this session)`);
nothing is replayed to the terminal.
The projector-backed statusLine and the durable launch audit under `<runtime-root>/launch/` are unaffected
— they remain the live surfaces.
Unlike the launch audit, the host log is diagnostics, not audit:
workspace removal (`--drop`, `ride clean`) deletes it with the workspace.

### Worker containers

A registered worker type can return a core `Container(WorkerContainer(…))` run without importing ride.
The shipped `webview` type is a browser worker container driven through `webview open`, `mission ask`, and `webview close`;
its optional noVNC loopback view requires `webview.vnc`, its page downloads require `webview.downloads`, and every webview launch requires `webview`.
`webview open --cookies <instance>` passes one `cookies` profile into the worker's scoped store, while `--vnc --port <port>` requests the noVNC host port.
Its declaration ships a byte-valued Docker build context whose normalized relative paths include a `Dockerfile` opening with `ARG RUNTIME_IMAGE` and `FROM ${RUNTIME_IMAGE}`.
It also declares a command, environment, and a mapping from distinct container ports to requested host ports.
A `null` host port asks the host to select an available one;
a requested host port must be distinct and at least 1024.
The environment cannot claim host-owned `BROKER_*`, `RIDE_*`, `BRO_*`, `HOME`, or `PATH` names.
The runtime image tag and sorted build-context files determine the worker image tag `bro/<type>:<hash>`.
Ride builds a missing tag lazily under the same host-wide image-lock protocol as runtime images and prunes only unlocked predecessors from that type's image repository.
The root process holds each worker tag it reaches for until it exits, so no other ride prunes an image its worker may use.
A failed build reports the captured output tail with its exit status.

A host command can instead run a declaration as a foreground worker run outside any ride.
It freezes the invoking installation, resolves and holds the runtime and worker images under the same image locks, builds missing images, and prunes none.
It mounts only the read-only runtime volume, publishes every declared port on launcher loopback at its requested or selected host port, and passes the mapping through `RIDE_PUBLISHED_PORTS`.
The command runs with stdin and stdout piped to the caller and no workspace, credential store, broker, or artifact view;
leaving the run removes its container.

The host lowers each broker run off the broker loop into a detached throwaway boxed workspace named `<type>-<channel>`.
It hydrates exactly the credential instances in the admitted descriptor's credential options into the worker's scoped store, selecting each instance as its kind's default there.
A missing instance fails the launch by name and removes the throwaway workspace.
No credential install hook runs in a worker container.
It mounts the frozen runtime volume read-only and the worker's read-only artifact view at the absolute normalized POSIX path the declaration names;
every accepted `share` ref reaches the worker before start and appears in the view once the worker fetches it.
The view path defaults to `CONTAINER_ARTIFACTS_ROOT` and is a launch fact, so changing it does not rebuild the image.
It mounts no session, party, or trails state.
The declared command runs as `broxy run -- <command…>`, so its clients multiplex through the worker's one upstream attach.

For each declared container port, the host binds its requested launcher-loopback port or reserves an available one when the declaration gives `null`, releases the probe, and creates the container with `-p 127.0.0.1:<host>:<container>`.
A requested port already in use fails the launch, and a process taking any selected host port before `docker start` makes the launch fail rather than moving the binding.
The worker receives the selected mapping as `RIDE_PUBLISHED_PORTS=<container>=<host>,…`, reports any usable address to its owner over its granted talk, and exposes the same host/container pairs through peer facts and the launch audit.
Docker supervision captures a bounded merged output tail;
a clean exit removes the throwaway workspace, while a failed or killed worker keeps it and includes the tail in the synthesized death result.

### Summoning another bro

A session can summon another bro over its channel.
The summon surfaces are wrappers over `launch {type: bro, …}`, the same request kind every registered worker type uses.
The target runs as a one-shot, non-TTY session that either starts a party of its own or joins the summoner’s party.
A *manual* summon instead has the user launch the child themselves either interactively or one-shot;
see "Manual summon" below.
The [permission reference](permissions.md#start-surfaces) defines the child's credentials, authority, placement, and authorization.
The service tool takes `placement=boxed|unboxed|join`; the CLI selects it with `--boxed`, `--unboxed`, or `--join`.
An unmarked summon is boxed, never an implicit join or an unboxed fallback.
A join inherits its party's isolation and refuses `into` and `manual`.
The root launch's `--env` additions reach started children and joined members.
Every harness runs a started child through `do-ride solo …`.
The started-party lowering emits `DockerLaunchSpec` or `ProcessLaunchSpec` through the common launcher roots use.
An unboxed join emits a member `ProcessLaunchSpec` in the summoner’s existing tree with an explicit environment snapshot and loopback broker upstream.
A boxed join emits a member `ExecLaunchSpec`:
a `docker exec -i -u ride -w <party start directory>` client into the party’s running container, running `env -i` with an explicit baseline (`HOME`, `PATH`, `TERM`, `LANG`) plus the member env
— an exec otherwise inherits the container’s config env, the party’s first session’s entire launch environment
— with `BROKER_UPSTREAM` under `host.docker.internal` and the member’s records reached through the party mount (`RIDE_SESSION_DIR=/var/ride/party/<member>/session`, and the harness's own state under `/var/ride/party/<member>/`).
The member’s scoped store is `docker cp`’d into the running container at a per-member path under `/home/ride/.bro-party/` and re-owned the way the entrypoint re-owns the root’s.
The exec client’s death does not end the process inside, so the exec’d command records the member’s pid and start time under its session dir before it execs `do-ride`;
the handle treats that record — or the client’s exit — as the started handshake, and `do-ride` removes the record at exit.
An unboxed child starts in its own process group;
kill sends SIGTERM only to `do-ride` so its harness-specific shutdown can unwind and flush state, then sends SIGKILL to the group if the process tree or inherited output pipe survives that grace period.
A boxed member’s kill follows the same TERM → grace → KILL shape through `docker exec`, signaling only while the container’s `/proc` still shows the recorded start time, so a late kill finds no target and never a reused pid.
The request’s complete `llm` text replaces the child’s configured recipe within that harness and never switches it;
the child inherits the root session’s attachment through its type.
A child with a `repo` attachment bases on the request's `into` ref when given.
Otherwise a started child uses the summoner's clone `HEAD` read at summon time, and a manual child reads that clone's `HEAD` when the operator launches it.
A joined summoner uses its party's clone.
Local-only commits are transferred into the attached checkout or mirror before the child's host-side clone is prepared;
uncommitted changes never transfer.
A detached root starts detached children and rejects `into`.
A joined child uses the party’s existing tree directly, including its current uncommitted state, and runs no workspace setup or persona provisioning of its own.
The quest carries a fixed **talk** over four rights:
`owner.say`, `owner.question`, `worker.say`, and `worker.question`.
A reply is authorized by the other end's question right.
A summon defaults to `worker.say` and widens only through the request's `talk` field, spelled `--talk <right>[,<right>]` by the CLI;
the host fixes and enforces the set when the quest opens, while both peer surfaces refuse a forbidden move before sending.
The answer is retained in the quest journal, while permitted messages can travel for the quest's whole life.
A quest also ends before its answer when its summoner is gone or says so:
a summoned session's exit, or a manual session's disconnect, ends every quest it opened as `failed:orphaned` and ends their workers down the tree, killing a spawned child and detaching a manual one,
and the `cancel` kind ends any live mission its owner names as `failed:cancelled` the same way.
A nested bro summon stamps the child trail's `summoned_by.trail_id`;
a root session summon omits provenance until the session recorder publishes its current trail id.
The UX is the shared `spell::ask` spell (`bros/bro/spells/ask.md`, inherited by every bro);
underneath it are two client surfaces over the same request, each split into the summon that opens a quest and the verbs over the quest that exists:

- `summon <target> <prompt>` and `quest <verb> <quest-id>`, for Bash-capable sessions.
  `summon` is blocking by default (quest id + started trail id on stderr, answer on stdout, non-zero exit with the reason on failure);
  `--start` / `--join` / `--boxed` / `--unboxed` select placement.
  The forwarded fields are `--timeout <s|none>` / `--into <ref>` / `--hold <level>` / `--grant <name>` / `--revoke <name>` / `--limit <entry>` / `--pass <kind>+<instance>` / `--share <ref>` / `--talk <right>` / `--harness <name>` plus the LLM flags;
  an omitted hold leaves the child's unattended default.
  `--share` hands the child read access to an artifact ref — see "Sharing artifacts between peers";
  `--export` starts it with `bro.export`, which lets it publish refs to the host.
  Grant/revoke/limit shape the child's authority; bare credential kinds in grant/revoke add or drop kinds.
  Harness and LLM flags shape the child's driving loop without answering to the summoner's credentials;
  the target's own host-config entry supplies whatever credentials that choice needs.
  A blocking wait rides through child says.
  When `worker.question` is granted, a child's question instead prints on stdout and exits 4 so the summoner gets a turn;
  stderr names the question and the exact `quest say <quest> '<answer>' --reply-to <question>` command that answers it.
  `--detach` waits for the first correlated message:
  host `accepted` prints the quest id, while a denial or pre-acceptance launch failure exits with its reason and prints no id.
  Any summon is reclaimable by that quest id, detached or interrupted, and `self` names the session's own quest on every verb that reads a conversation or talks.
  `quest check <id> [--wait] [--timeout <s>]` reads the outcome alone through a non-destructive journal query:
  it prints the answer and exits 0 once the quest ended well, exits 3 while it runs, exits 4 with the open questions a direct child is stalled on, and exits 1 with the reason when it failed or was denied;
  `--wait` loops bounded `query {id, wait, since}` reads until the end or such a question, and `check self` is refused.
  `quest history <id> [--wait] [--timeout <s>]` reads the conversation:
  the talk rights and the retained tail with every open question marked `pending` in place and `truncated` when older entries were dropped;
  it exits 4 when a question awaits the caller, and `--wait` returns on the next message or the end.
  `quest say <id> '<text>' [--reply-to <question>]` sends a message that expects no reply, or a reply;
  `quest ask <id> '<text>' [--reply-to <question>] [--wait [<seconds>]]` asks a question and prints its id, a counter-question with `--reply-to`;
  `--wait` prints a plain reply and exits 0, or prints a reply that is itself a question and exits 4, with stderr naming its `counter_question_id` and the ready counter-question command.
  When the optional wait bound passes first, it prints the original question id and exits 3, the reply still recoverable from the journal.
  Concurrent waiters and later reads see the same state.
  `quest list` walks the caller-scoped paginated `query {}` listing and prints retained bro summons live-first, including their talk and pending questions.
  `mission watch` first takes the current `events {}` head, then replays retained own-mission messages and open questions of every mission this session launched whose journal sequence is no newer than that head, marked `before the watch`.
  It then long-polls ordered lifecycle and chat events of those missions after the head, so chat committed between the head and replay queries arrives once through the stream.
  A mission launched by another session in the stream raises.
  A child mission is named `mission <id> (<type>[ to <target>])`, and its says, questions (with their id, and the id a counter-question answers) and replies (with the id they answer) follow the name;
  on the session's own mission it renders the other end as `summoner` and never echoes the session's own says or its own `listening` mark.
  A child's service marks (`accepted`, `started`, `trail`, `listening`) and says are quiet lines;
  questions, replies, refusals, ends, denials, gap notices, and the summoner's says to the session wake it.
  An optional worker type narrows the child missions it streams.
  An event-retention gap prints a notice, re-arms from the current head, and repeats the retained replay.
  `quest cancel <id> [--timeout <seconds>]` ends a bro quest this session owns and waits for it to end;
  it exits 0 once the quest has ended and 3 when the bound passes first, the end still on its way.
  `mission check|history|say|ask|share|list|watch|cancel` is the corresponding universal surface for every worker type:
  Chat payloads are JSON objects, and `history --seq N` recovers one full retained entry.
  A blocking `mission ask` has the same exits and reply handling as `quest ask`, printing a JSON payload and putting `<json>` in the ready counter-question command.
  `share <id> <ref> [--timeout <seconds>]` hands an owner-reachable artifact to a live worker.
  List and watch accept `--type`, and watch cuts chat lines over 1 KiB with a history pointer.
  `rewind show <trail-id>` peeks at a long summon mid-run.
  Contract details in `bro/summon.py`, `bro/mission.py`, and `bro/quest.py`.
- Every harness mounts the same service tools:
  `bro::summon`, then `bro::quest_check` / `quest_history` / `quest_say` / `quest_ask` / `quest_share` / `quest_list` / `quest_cancel` on the `quest_id` it returns,
  and `bro::quest_watch(wake_on_quiet)` where the session may summon, setting its runtime-owned session watch to wake on its quiet lines or not.
  The summon tool's `passes` list is the CLI's repeatable `--pass` field.
  `summon` returns the accepted state after host acceptance;
  answers, questions, replies, refusals, and terminal states arrive through the session watch.
  `quest_say` sends and returns, `quest_ask` mints a question id whose reply arrives through the watch, and `quest_share` hands an owner-reachable ref to a live child.
  `quest_check` and `quest_history` are one non-blocking journal read each;
  `quest_cancel` returns once the host accepts the cancellation, with the terminal following on the watch;
  and `quest_list` is the paginated journal listing.
  The service tools carry no waiting or detach controls.

### Manual summon — a child the user launches

A `manual: true` summon (`summon --manual`, or the `summon` tool's `manual` parameter, which never blocks for the answer) inverts the launch.
It takes the same `talk` / `--talk` widening and `passes` / `--pass` credentials as a spawned child.
The interactive session can exchange only the roles fixed at registration.
The summoner needs no placement right: the request refuses `placement` because the operator's launch owns it.
The host spawns nothing and instead registers an *expected external peer*
— a provisioned broker channel awaiting a child someone else starts
— and the quest id doubles as the launch token.
The registration is acknowledged with an `accepted` mark once the token is claimable, and the manual client waits for it, so a denial fails at the summon itself
— a token is only ever handed out for a summon the launcher is expecting.
The summoner relays `<runtime>/venv/bin/ride along --summoned <token> <target>` as the default launch command for an interactive session, using the ride's own host runtime.
For a one-shot request, the user can instead run `<runtime>/venv/bin/ride solo --summoned <token> <target>` and leave the run unattended.
Both are otherwise normal launches
— boxed or unboxed, any harness, the user's own `--llm`/`--hold`/`--workspace`/`--cred` and credential `--grant`/`--revoke`
— except they start no broker of their own.
The omitted hold follows the selected mode's default:
`along` is attended when boxed and guided when unboxed, while `solo` is unattended.
The launcher puts the summoner's provisioned channel in `BROKER_UPSTREAM`, and the session broxy publishes the local `BROKER_CHANNEL` its processes use to attach as a regular summon peer.
Its own nested summons therefore route through the summoner's control with per-peer authorization.
The [joint-start rules](permissions.md#manual-children-joint-starts) define prepare, operator commit, and generation-bound claim.
The request fixes the target, prompt, talk, gift/cap, attachment, base, and party environment.
At commit the operator may replace driving-loop choices and credential picks, but not authority entries or `--env`.
The final descriptor is checked again under the root's reading, and its exact credential values are hydrated.
The CLI does not expose timeout selection for a manual child, which has no host-owned timer.

The bridge between the two halves is the generic pending record (`ride/ride/pending_launch.py`),
written under `<runtime-root>/launch/pending/<token>.json` when the channel is provisioned and one-shot-claimed by the launch as its last fallible step before the session starts.
The record carries the ride runtime as its frozen bundle hash or given path.
A `ride along --summoned` entered through another installation reads that field first and re-executes from the owning runtime before it loads the rest of the record.
The child still completes the broker attach revision check after re-execution, so a missing or mismatched wire revision is refused before any envelope can be misparsed.
A second launch on the same token fails loudly (two sessions must not share one channel), and a summon that ends unclaimed (root teardown, a failure) discards it, so a stale token fails the launch with the reason.
The claim records the user-chosen workspace name beside it (`claimed/<token>.json`), which is how the host attributes the manual peer
— the base-ref source for the child's own summons and the tree its artifact mints resolve against
— so attribution comes from the launch machinery on the launcher, never from anything the child says on the wire.
Before the claim, a nested summon from the child is denied with a retry hint.
The child's harness announces the trail mark (`{trail_id}`) once the child's trail opens.
The `answer` service tool is mounted in every summoned session with a channel.
An interactive child calls it once when the work is done, a step its hold treats like any other
— confirmed first only under a hold that confirms each significant step
— and the session ends with the quest's ok result delivered to the waiting summoner.
A one-shot child follows the same lifecycle as a spawned child:
a clean exit delivers its printed reply, while `answer` remains available when the run needs to end by delivering explicitly.
An interactive session the user quits without answering produces no result, and the channel's EOF surfaces to the summoner as the synthesized `result{failed, reason: disconnected}` (channel EOF is an expected peer's death signal:
there is no process for the host to reap, and the session's broxy holds one upstream connection per run).
Root exit *detaches* an in-flight manual child rather than killing it
— the user's session lives on, un-summoned, its channel gone.
The summoner's side is the ordinary detach flow:
the token works with `quest check` / `quest list` / `mission watch`, reading as running until the user launches.

### Summon allowance

Every mission records how many more generations may start below it.
Launching any worker type requires an allowance of at least 1:
a summon, a joined bro, a webview and a benchmark all consume one generation.

A root takes the last `summon-depth` in its installation's home settings, host `defaults` and `homes.<installation home>`, or the framework default when none sets it.
A bro's cap is the last `summon-depth` in its bro layers:
its foreign home entry, when applicable, then the installation's own entry.
The root's allowance is lowered to that cap when present.
Every launched mission receives its launcher's allowance minus 1, lowered to its own cap if it is a bro.
A cap never raises an inherited allowance;
a cap of 0 prevents that bro from launching anything, including when it is a root.

Manual preparation does not check the allowance;
the operator's commit checks the summoner's allowance before admitting the child.
A refused launch names the allowance and the configuration sources that set it.
A resumed session is a fresh root and receives a root allowance from its new reading, not its previous inherited allowance.

### Host launch records

Host side, `PeerFacts` (`ride/ride/peer_facts.py`) holds a generic `WorkerFacts` row keyed by the mission a peer undertakes:
type, workspace, the session records directory where it is not the workspace's own, expected/artifact-view state, published ports, summon allowance with its sources, and a type-owned extension.
The extension is the `bro.worker_types.Extension` a type's run carries, and its `view()` is the JSON the host lists and audits for it;
the bro extension carries its name, resolved placement, and a joined member's name.
The journal's host-anchored mission seeds the root row;
an authorized launch adds its worker row before starting, with a started bro’s channel-named workspace or the claimed workspace for an expected bro filled at launch;
a joined bro’s row carries its inherited workspace and its channel-named member's `party/<member>/` records directory.
Every owner resolves through one join
— peer to undertaken mission through the dispatcher's worker binding, then mission to facts row
— and depth is the journal ancestry length.
`LaunchControl` (`ride/ride/launch_control.py`) maps requests and checks normalization, use, gifts, and admission against host mission records before worker existence checks and lowering.
A child has an independent base with explicit gifts and caps; its [authority record](permissions.md#resume-and-durable-records) is not copied from the parent.
The root's reading fixes configuration and recipe defaults for the whole tree.
A peer the control cannot attribute to a mission is denied.
Every launch is subject to the [summon allowance](#summon-allowance), independently of its authority.
Denials reply immediately and land in the journal and audit as `denied` transitions (reason, quest id, summoner, and bounded request args).
Each spawned child records `summoned_by` provenance from the summoner's current trail plus the summoning bro's own `tool_call` step id when the request carries one.
Owner attribution has one shape in the audit: `{workspace, type, extension?, trail_id?}`, with the owner's extension view.
The trail is read from the pointer under that session's records directory for every request because a harness may move a session to a new trail mid-run, with the answered quest’s journal `trail` mark as fallback.
The authorized spawn carries its `SummonSpawner` into the dispatcher with the requesting peer as parent.
`SummonSpawner` lowers the request off-loop through the started-party launcher or joined-member builder, then dispatches its concrete Docker, process, or member-exec description;
a grandchild’s lifecycle routes to the child that summoned it, and root exit still tears down the whole tree.
Every event lands a host log line and a durable audit row under `<runtime-root>/launch/<name>.jsonl`.
Each row keys the ride under `ride`, using the root workspace name.
Each entry names its actual `owner`, worker `type`, `published_ports`, bounded request args, transition, trail id, and terminal outcome.
Type-owned audit fields are isolated under `extension`, as the extension's view at that event:
a join reports the isolation it inherits and its member name from its spawn on, while a manual summon's isolation stays null because the user's launch settles it.
Live readers never read that audit back:
check, list, watch, and the session-local statusLine projector query the caller-scoped in-memory journal over their own broker channel, so the same surfaces work at any summon depth.
Quest verbs keep the bro workflow's type filter, while the mission surface, turn-end guards, and statusLine account for every owned mission.
The statusLine places a child's pending question beside its live summon, renders another live launch as its worker type, and keeps the most recent terminal outcome briefly visible.
The scope is the quests the caller requested and nothing beneath them;
it excludes the parent-owned quest that the caller's own worker answers.
A peer reads its live descriptor and authority through its broker channel; the banner exposes that view without participating in enforcement.
Root exit kills in-flight children with a loud log naming what was killed;
a result lost that way stays recoverable from the child's trail.
A SIGTERM to the launcher ends the ride the same way:
the root run reports the signal's exit code and its teardown kills the tree, the members running in sessions of their own included, rather than the signal orphaning them.
A started-child supervisor normally removes its throwaway workspace after a clean exit and retains it after failure or kill for inspection and recovery.
Credential-operation records are an additional [retention condition](permissions.md#credential-changes-and-settlement).
A joined-member supervisor removes `party/<member>/` only after a clean exit and retains it after failure or kill;
the member has no workspace or resume record, so its trail is its recovery surface.
A party ends with its first session:
that session’s teardown kills any members still running, loudly, whichever spawner runs them.
The unboxed process handle always removes the private credential state separately, and a failed removal fails teardown rather than reporting the child complete;
a boxed member’s store lives in the party container’s own layer and dies with it.
Each authorized start records the child’s run as its `broker-<channel>` workspace’s resume record
— the same solo session spec a `ride solo` launch under the child’s harness would record
— so `ride list` shows the child under its prompt and a surviving workspace resumes like any kept solo workspace:
`ride resume broker-<channel>` opens an interactive session continuing the child’s conversation under its harness (see "Lifecycle verbs").

### Sharing artifacts between peers

Peers pass files by content-addressed reference through the ride's store the launcher owns (`ride/ride/artifacts.py`; the wire contract, ref grammar, and CLI are `bro/artifact.py`):

- `artifact mint <path>` ingests a file or directory inside the minting peer's tree, named relative to its working directory or by an absolute path
  — a private reflink-or-copy, so nothing the producer writes afterwards changes stored bytes
  — and prints its ref, `<digest>/<name>`.
  The digest is `sha256:` plus the content digest for a file (so `sha256sum` checks it) or the digest of a canonical typed-entry manifest for a directory;
  the name is the path's last component unless `--name` gives another
  (`artifact digest <path>` computes the same ref locally, which is how a ref is verified end to end).
  The name is not digested, so passing a ref passes its name, and re-minting unchanged content under any name stores nothing new;
  a mint past the ride's byte cap is refused rather than evicted.
  The client sends an absolute path without resolving symbolic links;
  the host maps the peer's view to its tree, resolves the path and refuses one outside the resolved tree, including a symbolic link out of it.
  From a clone at `/tree/bro`, `artifact mint report.md` reads `/tree/bro/report.md`, and `artifact mint /tree/review/round.md` reads a report beside the clone.
- Reaching a ref materializes nothing, in any placement:
  `artifact list` prints every ref the requesting peer can read, one per line in the order they reached it,
  and `artifact fetch <ref>` materializes one and prints the path it appears at, the ref itself under the peer's artifact root, so the content keeps its name and two refs never collide.
  A boxed peer reads it under its declared artifact-view path
  — `CONTAINER_ARTIFACTS_ROOT` unless its worker-container declaration chooses another absolute normalized POSIX path, as webview does with `/tree/artifacts`;
  the fetch hardlinks it into the per-peer view directory, which is bind-mounted read-only, so it appears without a remount and writes fail with `EROFS`
  — while an unboxed party, having no mount namespace, gets a private copy under the workspace’s own `artifacts/` directory, shared by its members.
  Either way the path is not for editing in place;
  a peer that wants an editable copy makes one.
  A repeat fetch answers the materialization already there;
  `--checked` digests it first and replaces one that no longer matches the ref, failing when the stored content itself no longer does.
  The `artifact::` toolset serves the same `fetch` and `list`, and its `read` and `grep` take only refs the session fetched.
- Reach follows the launch tree, and nothing a peer says widens it:
  a mint is readable by the minting peer and its summoners up to the ride root;
  a launch request's `share` list (`summon --share <ref>`, the service tool's `share` field, or another worker type's corresponding launch argument) hands refs the owner itself can read down as the mission opens;
  and `mission share <id> <ref>` gives one such ref to a live mission owned by the caller,
  provided the mission's talk carries the owner to its worker at all
  — `owner.say`, `owner.question`, or a reply to a `worker.question`
  — since a worker polling `artifact list` would read the share as a message its talk withholds.
  Where the talk lets the owner start a message (`owner.say` or `owner.question`), the share also reaches the worker's session watch as a `summoner shared artifact <ref>` line.
  There is no other path
  — knowing a ref is not access, and a denial is uniform whether or not the ref exists.
  Reach is to the digest, so a peer that may read the content fetches or shares it under any name.
  A manual summon refuses launch-time and live `share`, and a manual child's `fetch` is denied
  — the launcher builds no launch for it, so no view is mounted
  — while it lists what it reaches and its mints flow upward normally, attributed to the workspace its own `--summoned` launch claimed the token with.
- The store is ride-scoped and dies with the ride
  — a resumed root starts a new ride with an empty store, so a stale ref fails at its own `artifact fetch`
  — while mints, fetches, shares, and denials outlive it in a JSONL audit under `<runtime-root>/artifacts/<ride>.jsonl`, beside the launch audit.
  Each audit row keys the ride under `ride`.

- The host side is the operator, who reaches the store through the root's operator channel (`ride artifact`, re-executing into the root's runtime like `ride override`):
  `ride artifact share <mission> <path-or-ref> [--name N]` ingests a host file or directory, or takes a ref the operator can already read, and shares it with any live mission, which `<mission>` names by the same mission id `ride override` takes;
  the mission's session watch announces the share as an `operator shared artifact <ref>` line.
  A member of a joined party shares its summoner's view, so the whole party reads what is shared with one of them.
  `ride artifact fetch <ref> [--to <directory>] [--root <root>]` copies a ref the operator can read to `<directory>/<name>` (the current directory by default) and prints the path, refusing to replace an existing one;
  `ride artifact list [--root <root>]` prints those refs one per line;
  `--root` is needed only when several roots are live.
- The operator reads only what a session exported or the operator itself shared.
  A session exports with `artifact export <ref>`, which needs the session to have started with `bro.export` (`ride along --export`, `summon --export`, or the summon tool's `export`) and the ref to be one it can read.
  An exported ref reaches no other peer.
  Exports, operator shares, and operator reads are audited with the rest.

Worker types resolve accepted shared refs through `Host.artifacts`, under the same sharing check.

### The launcher↔session contract

The launcher and `do-ride` come from the same frozen or explicitly given runtime, so workspace age and the installation that first received a manual token cannot skew their contract, and the operated repository need not install either command.
`ride clean` sweeps idle workspaces, unreferenced managed mirrors, unlocked runtime bundles, and the unused host caches each installed harness cleans through `SessionHarness.clean_cache`;
removing a bundle also removes its unused runtime volume, while Docker keeps an in-use volume alive.

## The session executable

`do-ride solo|along --workspace NAME --harness H [--resume] [--attachment JSON] --hold HOLD [--] <bro> [prompt]` (`ride/ride/do_ride.py`) runs one session in a prepared workspace.
It has its own parser:
there is no `--in-place`, no outer machinery flags or combination refusals, and `--resume` is an ordinary session flag.
The launcher puts the bro and the prompt past `--`, so a prompt that reads as a flag stays the prompt.
Unboxed isolation runs it from the snapshot venv and boxed isolation from the mounted runtime volume;
both expose only pinned session shims plus system paths.
The distribution declares `do-ride` as both a console script and a session command, so every runtime bundle carries it beside `ride`.

`do-ride` receives `RIDE_ISOLATION`, the admitted recipe in `RIDE_RESOLVED_RECIPE`, and the attached tree's `RIDE_BRANCH` / `RIDE_BASE_SHA`.
It then exports the bro git identity, `RIDE_WORKSPACE`, `RIDE_REPO`, `RIDE_REPO_URL`, `RIDE_BRO`, and the `BRO_HOLD` / `RIDE_RUNNER_PID` pair (see "Forwarded env vars").
It installs the scoped credential hooks, runs repository provisioning followed by the persona's steps (`BaseBro.provision_workspace`) when attached and not joined, then asks the selected harness to prepare its session state.
The provisioning contract is in "Repository configuration".
It owns the optional session broxy.
While the harness runs, `runner.pid` under `RIDE_SESSION_DIR` records the executable's pid and operating-system start-time identity as JSON;
`do-ride` removes only the record it owns when the session exits.
Harness-specific preparation lives in `SessionHarness.prepare_session`, followed by `run_session`, so the neutral executable compares no harness names.

Every harness tells its session the hold up front, rendering the `--hold` level's text (`bro/prompts/AGENTS.md`, "Hold text") into the prompt its model starts under,
so a session never detects its hold at runtime.
Each session also names its agent's cumulative LLM usage to the commands it runs, in the usage file `BRO_USAGE_FILE` points at (`bro.llm.usage`), which the commit footer reads;
the harness keeps that file current.

### Bro spells and skills

A bro's spells (the files its `spells` declaration names, `bro/reference/extending.md`, "Declaring a bro") are canonical `spell::<name>` tools;
`bro::cast` joins the service server when `mu` resolves, and the Spells contract routes `[[…]]` markers to it where it is mounted and to the spell's own tool otherwise.
How a session receives the Spells contract and loads third-party skills is its harness's own.

## Forwarded env vars

Wrappers and session daemons rely on a small set of env vars:

- `RIDE_WORKSPACE` — workspace name.
  Set by each launcher and overwritten from `do-ride --workspace`, so no ambient parent value survives.
  `ride banner` reads it to render the session header.
- `RIDE_ISOLATION` — the workspace's recorded `boxed` or `unboxed` isolation.
  Every launcher sets it explicitly for the session and its children.
  `ride banner` derives the container path and Docker shell command from it rather than probing `/.dockerenv`, and a trail's `location.is_container` is the same fact.
  `do-ride` uses the same fact for isolation-specific setup, so an unboxed ride launched inside a foreign container remains unboxed.
- `RIDE_REPO` — the root session's resolved checkout path or normalized git URL, absent when detached.
  The `repo` type contributes it to the launch environment;
  the banner reads that fact rather than deriving a repository from cwd.
- `RIDE_REPO_URL` — the attached session tree's sanitized network `origin`, absent when detached or when `origin` is a local path or `file://` URL.
  The `repo` type reads it from the prepared clone for each launch.
- `RIDE_BRANCH` — the attached workspace's recorded branch, absent when detached.
  The `repo` type reads it from its attachment record.
- `RIDE_BASE_SHA` — the commit the attached clone starts the session at, set beside `RIDE_BRANCH` and absent with it:
  the resolved base a fresh clone is made at, or the clone's HEAD for a resume and for a joined member.
- `RIDE_HOST_TREE` — launcher-side absolute path to the workspace tree, set explicitly in both isolations.
  It is normally `<runtime-root>/workspaces/<name>/tree`, or the recorded external path for `--tree`;
  in a container it names the host path bound at `/tree`.
- `RIDE_HOST` — the launcher's hostname, set explicitly in both isolations.
- `RIDE_RUNTIME` — launcher-side absolute path to the ride's materialized host runtime.
  Manual-summon surfaces use its `venv/bin/ride` so the child starts from the same runtime.
- `RIDE_COMMAND` — the user-visible invocation this session launched under for telemetry and the banner:
  the reconstructed `ride solo|along …` command with its flags, `ride resume [--llm RECIPE] <ref>` for a resume, or the `summon --join …` request that started a joined member.
  Set by the launch env of every started session and by the join lowering for a member.
- `RIDE_BRO` — names the bro the session runs as (the selected bro).
  Set explicitly in the container env at every boxed launch site
  — a `ride along` container carries its session bro, a bro-harness container or summon child the launched bro (`ride/ride/bro_worker.py`)
  — and exported by the session executable layer;
  never admitted from the ambient environment, so a calling session's value never leaks into a child that runs a different bro.
  Purely a theming output
  — the banner's ASCII Bro logo + bro-name header, the statusLine
  — never an input:
  the session's bro identity travels in the spec's own flags.
- `BRO_HUMAN_NAME` / `BRO_HUMAN_EMAIL` — the human the session works for, as the attached repository's `user.name` / `user.email` name them on the launcher (`ride/ride/identity.py`).
  Contributed by the `repo` type in both isolations, including children
  — whose repository is its summoner's, so the two credit the same human.
  Absent for a detached launch, which has no commits to credit, and for an attachment declaring no identity
  — which the launch warns about.
  The names are owned by `bro.workspace.human`, whose `session_human()` reads them back:
  a session commits as its bro, so the human it credits reaches its commits only through what the launch carried (`bro.workflow.co_author`).
- `BRO_HOLD` — the session's user-involvement level (`unattended | detached | attended | guided`);
  the neutral `do-ride` layer exports it from `--hold` for every harness, overwriting any ambient value (a session launched from inside another must not inherit its hold), before anything the session spawns inherits the environment.
- `RIDE_RUNNER_PID` — the `do-ride` process's own pid, always (re-)exported next to `BRO_HOLD`.
  Its pid and operating-system start-time identity also live in `RIDE_SESSION_DIR/runner.pid` while the process owns the session.
  The `raise` service tool's kill target:
  SIGTERM to the runner ends the harness process while the runner survives for its teardown, which is how an unattended session's raise terminates the run
  — reporting the status the tool left in the session state dir rather than whatever the harness process exited with.
  Its presence co-gates the tool's mount
  — without a runner to signal there is nothing to terminate.
- `RIDE_SESSION_DIR` — the session’s own state directory (see "Workspaces"):
  the workspace’s `session/` for its first member, or `party/<member>/session/` for a joined member;
  unboxed isolation uses the absolute path, while boxed isolation reaches it through a container bind.
  Set by every managed launch for every harness
  — `ride`’s own in both isolations, and a summon’s child spawn.
  `do-ride` requires it before starting the harness.
  Read by `bro/monitor` — a process without it is in no managed session and so has no trail pointer to publish and no recording health to report.
- `RIDE_SUMMONED` — marks a run as a summoned child.
  The env name is owned by `bro.summon` and read back through its `summoned()` predicate;
  set by the summon lowering and by the `--summoned` launch,
  read by the harness to emit the child's run lifecycle over the broker channel, by the service-server build to mount the `answer` tool,
  and by `ride banner` and `bro.prompts.session_fragment` so the run can tell in-session that it owes a summoner an answer.
- `RIDE_PARTY_MEMBER` — the channel-derived member name when a session joined an existing party.
  Its presence makes the banner and session prompt state that the tree is shared, and makes `do-ride` skip persona workspace provisioning.
- `RIDE_TRAILS_ROOT` — names the local trails backend's root (`bro.workspace.paths.trails_dir`) to a session that cannot derive it.
  An unboxed session gets its ride's own root, since its closed environment carries no data home and an inherited value would be its parent's;
  a boxed party member gets its own `party/<member>/trails` under the party mount, since the ride-wide `/var/ride/trails` bind belongs to the first session and its scope may not have one,
  and the member's supervisor adopts those trails into the ride's own store when it settles.
- `BROKER_MISSION` — the broker mission this run undertakes.
  Every spawner writes it beside the channel;
  a manual summon's token is its mission id and carries the same value into the user-launched session.
  The bro workflow exposes this mission as the run's own quest, named `self` on its quest surfaces.
- `BROKER_TALK` — the sorted, comma-separated rights of the run's own quest;
  empty means mute and unset means the launcher published no fact.
  Every spawner writes it beside `BROKER_MISSION`, and a manual summon's pending record carries it into the user-launched session.
  `bro.summon.talk()` feeds the banner and the session prompt's `#talk` fact;
  the host journal remains the enforcing copy.
- `RIDE_PUBLISHED_PORTS` — a worker container's comma-separated `<container>=<host>` loopback port mappings, including an empty value when it declares none.
  Session containers do not receive it.
- `RIDE_IN_CONTAINER=1` — the runtime image's own, marking a process running in an image this runtime built.
  `bro/workspace/paths.py:trails_dir` uses it only to resolve the image's fixed trails mount.
  Session placement comes from `RIDE_ISOLATION` instead.
- `BRO_STORE` — the exclusive scoped credential-store directory delivered by the launcher.
- `BRO_INSTALL_KINDS` — the space-separated declared kinds hydrated at launch, including an empty value when none resolved.
  `do-ride` requires these two variables together and installs only those kinds' hooks.
- `BRO_INSTALL_DIR` — the session's directory for credential-hook output and its current environment overlay.
  Each launcher sets its own value, overriding ambient session state;
  a direct `do-ride` invocation derives the current isolation's session environment directory and exports it.
- `RIDE_RESOLVED_RECIPE` — the JSON record `{"recipe": <dump>, "source": <source>}` of the recipe the launcher admitted and its source.
  Starts, manual commits and resume records carry the same shape under `resolved_recipe`.
  `do-ride` requires it as the session's only recipe input, so the harness runs exactly what the launch admitted and recorded.
- `BROKER_UPSTREAM` — the host broker address supplied by a launcher, `tcp://<token>@<host>:<port>`.
  Only the session broxy consumes it (`host.docker.internal` in a container, loopback on host), removing it once the local proxy is ready.
  If proxy launch fails, it remains set while `BROKER_CHANNEL` stays unset, which makes `Client.from_env` report the failed session proxy;
  the launch warning names the session's `broxy.log` path.
- `BROKER_CHANNEL` — the address broker clients connect to, with the same URI shape.
  The broxy publishes its loopback address here after readiness;
  a launcher that intentionally runs no broxy may publish a direct channel instead.
  It is never used as upstream launch input or rewritten from one referent to another.
  It carries a credential, so it belongs in no log:
  `bro.broker.transports.tcp.redacted` is the form that goes in one.
  Read by `bro.broker.client.Client.from_env` — the `broker` CLI and bro's `RunLifecycle` ride it.
  Both broker variables unset means no channel was intended.
- `BROKER_DISABLED` — launcher-side presence-checked kill-switch:
  the session gets neither broker variable (see "The broker channel").
  Checked before broker machinery imports (`ride/ride/workspace/containers.py:broker_enabled`);
  the constants-only `bro.broker.environment` module may already be loaded.
- `SSL_CERT_FILE` — set for an unboxed session of a given runtime to the certifi store inside that runtime's venv (see "Runtime bundles");
  absent for a frozen runtime's sessions, and never admitted from the launcher's environment.
- `TERM`, `TERM_PROGRAM`, `TERM_PROGRAM_VERSION`, `COLORTERM`, `VTE_VERSION` — the launcher's terminal identity (`SESSION_TERMINAL_ENV` in `ride.runtime_bundle`).
  Forwarded in either isolation into a session attached to that terminal
  — an `along` root, or a manual summon child, which the user launches from a terminal of their own
  — and into nothing else:
  a solo run, a spawned child, and a joined member are attached to no terminal and get no identity for one.
- GitHub and AWS reach a session as the scoped `github` / `aws` secrets via their install hooks, not as forwarded env;
  an ambient host `GITHUB_TOKEN` is deliberately not admitted into either isolation.

## Session recording

A managed session records its trail through the backend the session's own `trails` credential selects, and to the local filesystem where that kind's unpicked empty instance is absent.
A selected or present `trails` name must load like every other held credential;
`--no-trails` removes the recording need, and the session records no trail.
For local storage, the launch description binds the host's `<runtime-root>/trails` at `/var/ride/trails` inside the container, so the trail survives container removal and is visible to host-side `rewind` while the session runs.

Whichever harness records it, the trail carries the launch recipe and the managed-session facts
— the session's location, its launch line, and the attached tree's state in the header's `git`, read off the session env by `bro.trails.record.session`
— and the session publishes its current trail id to its trail pointer (`bro/monitor/trail_pointer.py`),
from which summon control stamps the session's summoned children with `summoned_by.trail_id` (see "Summoning another bro").
Each harness writes a local record of its run, a native run log or Claude transcript, and a separate session recorder turns it into a trail.
The recorder beats the recording health file (`bro/monitor/health.py`) on every attempt, naming the log where a failure's cause is read, and `ride banner` surfaces it.
The beat carries the outcome and the age at which the reader must give the recorder up, so both a recorder that fails loudly and one that is simply gone
— killed by a signal, OOM, a crashed container process
— are reported while the session runs.
