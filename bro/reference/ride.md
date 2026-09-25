# ride

`ride` is the managed-workspace runtime.
It combines a harness implementation with a bro personality and prepares a boxed or unboxed workspace around an independent clone.
It hydrates the launch scope, supervises the root session and its summons, and records enough state to resume the workspace later.

The runtime is published by the `bro-ride` distribution and depends on the `bro` framework.
The framework never imports `ride`:
workspace mechanics, credential scoping, broker supervision, and bro composition remain reusable framework layers.

## Commands

> **Isolation boundary:**
> `bro run` and `bro chat` run immediately in the calling process with ambient credentials.
> They do not isolate, clone a workspace, or hydrate a scoped store.
> Use `ride solo` / `ask` for an isolated one-shot and `ride along` / `call` for an isolated interactive session.

### `ride solo <bro> <prompt>`

Runs one prompt without a TTY and writes the harness reply to stdout.
The default harness is Claude Code and the default hold is `unattended`.
Claude runs in print mode over stream-json with its stdin held open, so each turn's reply lands on the session's stdout and its diagnostics on stderr beside the ride's own log lines;
the bro harness runs `bro run` with the ask display preset.
Both retain the session record needed by `ride resume` when the workspace is kept.

A launch without `-w / --workspace` receives a fresh name and removes that workspace after a clean exit.
`--keep` retains it, and a failed run always keeps it for inspection.
`-w NAME` creates or reuses that exact workspace after checking its isolation and always retains it.

### `ride along <bro> [prompt]`

Starts an interactive session.
The default harness is Claude Code, the default hold is `attended`, and the workspace is kept after exit.
`--unboxed` runs the workspace tree directly on the launcher's filesystem and changes the omitted hold to `guided`, because a non-guided unboxed session skips permission prompts without a container boundary.

A launch without `-w / --workspace` receives a fresh name.
`-w NAME` creates or reuses that exact workspace after checking its isolation;
a pinned workspace cannot be combined with `--drop`.
For an automatically named workspace, `--drop` removes it only after a clean exit and keeps a failed session for inspection.

Both mode verbs start **detached**:
`/workspace` is an empty writable directory and no repository is cloned.
`--repo PATH|URL` accepts either an existing path inside a checkout (resolved to its root) or a scheme/scp-shaped git URL.
A path attachment bases on the checkout's current `HEAD`;
a URL attachment fetches its managed mirror and bases on the fresh `origin/HEAD`.
`--into REF` selects another branch, tag, or commit, requires `--repo`, and affects only workspace creation.
Uncommitted host changes never transfer.

The prompt occupies a positional slot on both mode verbs.
Arguments for the harness program therefore follow an explicit separator
— they reach the claude binary on the claude harness and the native `bro run|chat` argv on the bro harness:

```console
ride solo dev 'inspect the launch path' -- --debug mcp
ride along dev 'continue the inspection' -- --debug mcp
```

Shared launch flags are `--repo`, `--boxed`, `--unboxed`, `--hold`, `--cred`, `--grant`, `--revoke`, `--into`, `--no-trails`, `--env`, `--session-log`, and the LLM selection set (`--provider`, `--model`, `--effort`, `--fast`, `--llm`).
`--cred KIND+INSTANCE` picks the stored instance a held credential kind reads and never adds the kind.
`--grant` and `--revoke` use the framework's unified grammar:
bare credential kinds shape the scoped store, `@bro` names shape the summon allow-list, and `:permit` leaves shape party authority.
Worker permits have the form `:<type>.<leaf>`, with one or more dot-separated leaf segments.
The bro type declares `:bro.party.start.boxed`, `:bro.party.start.unboxed`, and `:bro.party.join`, while the webview type declares `:webview.vnc` for its human-visible loopback view;
the framework seed is boxed starts alone, `:bro` is malformed, and `:bro.party` names an undeclared leaf rather than expanding to its descendants.
`--no-trails` disables trail recording for the session, whichever harness runs:
the launch omits recording's optional `trails` need, sets `TRAILS_DISABLED` for the run, and a claude session starts no recorder daemon.
`--env NAME=VALUE` (repeatable) adds a variable to the session environment of the root and of every party member it summons:
it is the lowest layer of that environment, beneath the launch's own variables and the ambient inputs the launch admits, and the recorded session spec carries it, so a resume and every summon repeat it.
`--session-log LEVEL` sets the level the session's own processes log at
— `do-ride`, the session MCP server, the recorder daemon, and the hook scripts all read `BRO_LOG_LEVEL`
— by recording the `--env` addition `BRO_LOG_LEVEL=LEVEL`, so every party member and a resume carry it the same way;
the launcher's own `--log` never reaches the session.

### Lifecycle verbs

- `ride resume <workspace>` relaunches the recorded session recipe, with optional `--cred`, `--grant`, and `--revoke` adjustments.
  Resuming a solo run opens an interactive conversation in the same workspace and re-resolves the hold to `along`'s default (`attended`, or `guided` with `--unboxed`).
- `ride list` lists every workspace, its attachment, and its activity state.
  Live badges are `[o]` boxed and `.o.` unboxed;
  idle badges are `[-]` boxed and `.-.` unboxed.
- `ride clean` removes inactive clean workspaces, managed URL mirrors no workspace references, and unlocked runtime bundles;
  `--force` permits dirty workspaces and removal when an attached repository no longer exists, while `--dry-run` reports only.
- `ride exec <workspace> [command ...]` enters a running boxed workspace.
  The exec process is outside the running session's process tree and has no broker channel of its own.
- `ride check-clean <workspace>` reports whether removal is safe.
- `ride scope [--repo PATH|URL] [--bro BRO] [--harness HARNESS]` prints the prospective credential tiers, selected credential instances and choosing layers, and name-presence states.
  `PRESENT` means the selected name has material or a typed-source entry, `SKIPPED` is an unpicked optional empty instance that is absent, and `MISSING` would fail the launch.
  Detached scope requires `--bro`;
  an attachment may supply the project default.
- `ride banner [--llm]` renders the session facts.

## Harness selection

`--harness {claude,bro}` selects the driving loop.
An attached launch reads `[tool.bro] harness`;
a detached launch and an attached project that omits the key use `claude`.

LLM flags resolve within the selected harness.
They never switch the harness implicitly.
A recipe whose provider the harness cannot run errors with `--harness` as the remedy.

### Harness seam

`ride.harness.Harness` is the runtime boundary.
The neutral layer (`ride/ride/session.py`) owns one started-party launcher parameterized by workspace isolation.
It prepares either a container `Launch` or an explicit process launch, and a harness implementation supplies what differs:

- its `ScopeRecipe`, the auth preflight, and LLM resolution;
- the session run under `do-ride` inside the prepared workspace, consumed by both isolations;
- session existence with its resume-refusal wording, the subject read, and the session trail-pointer path;
- the boxed extras (env, mounts) and the unboxed runner-env preparation.

`scope_recipe` takes no session, so surfaces with no session
— `ride scope`, dive-in's task prefetch
— resolve their recipe through the same seam.

The generic scope computation and the bro-run recipe live beside the seam in `ride.scope`, so native launch and summon lowering share the same policy.
Claude's recipe remains private to `ride.claude`.

## Claude harness

A Claude session retains Claude Code's built-ins, skills, and base prompt while adding the selected bro's persona, spells, filtered MCP namespaces, and blocked-tool declarations.
It requires the `claude_code` setup token.

The outer runs the separate `do-ride solo|along` session executable.
Unboxed isolation takes it from the frozen host snapshot;
boxed isolation takes it from the same bundle materialized at `/var/ride/runtime`.

A solo session runs Claude in print mode over stream-json, its prompt delivered as the first user message and its stdin held open:
Claude then stays alive as long as stdin is open and starts a turn of its own when a background task finishes,
and the runner closes stdin at the first turn end with no background task running, which ends the session.
A solo session's settings therefore wire `ride.claude.stop_guard` as its `Stop` hook:
reading the running tasks off the hook input and every mission the run owns off the host journal, it blocks a turn end once per turn (Claude's `stop_hook_active` marks the second stop)
when missions are in flight with no background task running, where the exiting process would orphan them,
or when a background task is running with nothing in flight, where it would hold the session until killed;
any running task over missions in flight is the wait it should be and passes.
The stop guard reads `background_tasks`, which Claude Code sends undocumented;
`ride/ride/claude/stop_guard_llm_test.py` probes it live against the Claude Code release the container pins.

Claude Code notifies a finished background command with the path of its output file, never the output.
Every session's settings therefore wire `ride.claude.watch_delivery` as its `UserPromptSubmit` hook, which Claude Code also runs on a task notification:
for a notified `watch-next` it returns the output as additional context, so the woken model has the watch's lines without reading the file.

Claude Code runs a Bash command in a login shell whenever its startup shell snapshot is missing, and a login profile that resets PATH there, Debian's `/etc/profile` or the user's own, drops the session commands.
The runner therefore pins the shell through `CLAUDE_CODE_SHELL` and routes every command through a `CLAUDE_CODE_SHELL_PREFIX` script (`ride.claude.shell_prefix`) that restores the session's PATH first;
`ride/ride/claude/shell_prefix_llm_test.py` holds that fallback live against the pinned release.

## Bro harness

The bro harness drives the selected bro's native LLM loop:
its runner spawns `bro run|chat …` in the workspace and waits, forwarding SIGTERM to it.
Boxed sessions run the same `do-ride` command summoned children get;
unboxed sessions provision the workspace clone and run the runtime snapshot's `do-ride` under the same broker-root supervision and scoped credential store.

A native session that can receive summon traffic starts `quest watch` as a watch-mode `bro::job` once, and its output reaches the LLM as notifications after tool results or in an idle interactive turn.
`bro::chill` waits on the run's whole background-job inbox when no other work remains.
A one-shot `bro run` ends when a turn ends with nothing running and nothing in flight:
a turn that ends with a live job or an owned mission still in flight gets one framework notice through the same seam (a user-role item recorded as a `notification` step),
naming each `job-N <mode> <command>`, each bro mission as `quest <quest id> to <target>`, and each other mission by id and type,
and the run ends only when a turn ends with nothing live or when a reminded turn ends with the same set and no job news drained since.
The end still closes every job and orphans every in-flight mission, as a delivered `answer` or a `raise` does at once.
`bro chat` stays idle on the inbox and starts a turn when news arrives.

Each session publishes its own current-trail pointer beside the workspace's `resume.json`:
the native runner publishes when its trail opens, and the Claude recorder republishes as segments turn over.
`ride resume` continues that exact trail at its latest consistent point under the recorded native recipe, producing a new trail with `forked_from`;
it does not use the globally newest call or the bro class's current recipe.
A session with trail recording disabled publishes no pointer and cannot be resumed.

`bro run <bro> <input>` and `bro chat <bro> [what]` are in-process surfaces:
they use ambient credentials and create no workspace or scope.
`bro chat --fork [TRAIL_ID] [--at N]` explicitly forks recorded history under the bro class's current recipe;
an omitted trail id selects the bro's newest recorded call.
`ask` and `call` are aliases of `ride solo` and `ride along`, with no implied flags;
`summon` and `quest` are the self-contained peer clients.

## Boxed launches and Docker daemons

`ride` can launch from a physical host or from inside another container.
Before the first boxed root or child in a ride, it starts a throwaway runtime-image container and reads a nonce through a bind of the runtime root.
A daemon that cannot see the launcher's filesystem fails this preflight with its endpoint and the runtime-root path.
The nonce covers every boxed bind because each bind source must resolve under the runtime root;
a launch refuses an outside source rather than sending an unverifiable path to the daemon.
An unboxed launch needs no Docker daemon and works wherever `ride` itself runs.

## Runtime state

The runtime command is `ride`, and runtime-owned environment facts use `RIDE_*`.
Persistent runtime data lives outside every checkout under the user's global **`<runtime-root>`**:
`~/.local/share/ride/`, or `$XDG_DATA_HOME/ride/` where that variable names an absolute path.
A launch creates it with mode 0700 on first use.
Its top-level stores are `workspaces/`, `runtime/`, `repos/`, `trails/`, `launch/`, and `broker/`;
workspace metadata records which repository, if any, each workspace is attached to.

Workspace metadata is read only in its current strict shape.
A command naming a non-empty workspace directory without a current record, or one whose `workspace.json` does not match that shape, refuses the workspace by name and points to `ride clean --force <name>`.
That explicit force-clean path removes the directory without interpreting its metadata, after the same session-lock and running-container checks used for a current workspace.

### Runtime bundles

Every outer root freezes the Python installation that invoked `ride`, by each distribution's recorded provenance:
index installations become exact `name==version` pins;
version-control and remote-archive installations become direct-reference pins at their resolved commit or recorded hash;
local sources — a directory or an archive
— are carried in the bundle as wheels built or copied from their current contents.
A directory's wheel is built through its sdist, so a leftover in the tree, such as a stale setuptools `build/lib`, never reaches it.
Their entries are laid out in a fixed order and carry one fixed timestamp rather than the build time a backend may record,
so an unchanged source tree keeps resolving to the bundle it already froze.
The Python major/minor joins the manifest, and a content hash names the persisted bundle under `~/.local/share/ride/runtime/<hash>/` (or `$XDG_DATA_HOME/ride/runtime/<hash>/`).
Carrying local sources keeps the snapshot self-reproducing:
re-resolving from a materialized bundle's own venv lands on the same hash.
A distribution that cannot be pinned reproducibly
— a vanished local source, a version-control installation with no resolved commit
— fails the launch naming what to reinstall it from, instead of silently escaping the snapshot.
The root holds the bundle's shared flock until its session and summoned children exit;
`ride clean` removes only bundles whose lock is available, each with its runtime volume,
and sweeps up what a hard-killed ride can leave on the daemon:
labeled materializer containers whose bundle is gone or reclaimable, and runtime volumes no bundle directory names.

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
Materialization is where every pin is fetched, so a version-control or remote-archive pin has to be reachable from the materializing environment
— in boxed isolation that is the runtime image's own git and network, without the launcher's credentials.
The session PATH starts with the runtime shims and then the system paths in both isolations, with the launcher's active venv removed on the launcher.
Repository commands are explicit through `uv run` or `.venv/bin/`.
The shims serve the session;
the machinery a session spawns for itself
— the session MCP server, the recorder daemon
— is named by its path in the runtime the spawning process runs from and never looked up on that PATH, so the roster is what a session may run rather than what it needs to work.

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
A directory under the store that records no workspace is ignored by enumeration, so a stray entry cannot disable the lifecycle verbs:

```
<runtime-root>/workspaces/<name>/
  workspace.json      what the workspace is (below)
  tree/                empty when detached; otherwise an independent clone
  lock                the session lock ("One session per workspace")
  exit                how the last session ended (above)
  resume.json         the spec `ride resume` relaunches ("Lifecycle verbs")
  session.log         the launching process's mid-session output ("The session host log")
  session/            what the running session records about itself:
    current-trail.json            the trail the session records into ("Summoning another bro")
    session-recorder-health.json  the recording health signal ("Session recording")
    claude/                       claude harness artifacts (recorder/projector logs, live statusLine projection, and the persistent Claude temp root)
  claude/             the claude harness's state dir ("Unboxed Claude-state isolation")
  party/<member>/     records for a session that joined this workspace's party:
    session/          the member's session records, with the same shape as above
    claude/           the member's private Claude state
    trails/           a local-recording boxed member's trails, staged here then adopted into the ride's host store on exit
```

The tree sits in its own subdirectory rather than being the workspace directory itself:
a container bind-mounts it as `/workspace`, and the records must stay outside that mount.
Whichever harness ran it, everything a session leaves behind is one of these records, so removing the workspace directory is what reclaims it.
Of the last three, `session/` is the unconditional one
— every session of either harness records into it.
It is the only record the session reaches from *inside* itself:
unboxed isolation uses its absolute path, and boxed isolation uses a bind at `/var/ride/session`, either way named by `RIDE_SESSION_DIR`.
That reach is what it exists for:
the trail pointer and the recording health signal are published by the session and read back host-side, so both ends need one path.
Signals every harness shares sit at its root, a harness's own artifacts under `<harness>/`
— where the claude recorder's stderr goes.
Every unboxed session puts its scoped store and install-hook output under one private temporary root removed when the session ends, so retained or collected records hold no secret.
A box keeps their equivalents in its own layer.
`claude/` belongs to the claude harness in both isolations.
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
{"isolation": "boxed", "repo": "/home/me/project", "branch": "workspace-my-task", "throwaway": false, "tree": null}
```

- **`isolation`** — `boxed` (the default) or `unboxed` (`--unboxed`).
  It is fixed at creation;
  a launch naming an existing workspace must request the same isolation.
- **`repo`** — the resolved checkout path or normalized git URL for an attached workspace.
  It is absent when detached;
  reusing a name with a different attachment is refused.
- **`branch`** — the attached clone's branch, present if and only if `repo` is present.
  A new workspace uses `workspace-<name>`.
- **`throwaway`** — the workspace is disposable.
  Its supervisor removes it once its session exits cleanly.
  It is set for the workspaces summoned children run in.
- **`tree`** — the absolute external tree named by `--tree`, otherwise `null`.
  An external tree belongs only to a detached unboxed workspace, must exist outside the runtime root, and may be recorded by one workspace at a time.
  Resume fails if it disappears;
  workspace removal deletes only the records and never this directory.

A detached record is `{"isolation": "boxed", "throwaway": false, "tree": null}`.
A managed detached tree is clean exactly when it is empty;
an attached or external-tree workspace uses the recorded-session-end clean rule.
Removing an attached workspace whose recorded checkout or managed mirror no longer exists requires `ride clean --force`.

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
  One code path for every flag combination and both harnesses carries the session environment and persona provisioning, then hands off to the harness's runner
  — claude's `ride/ride/claude/runner.py`, or the bro harness's spawn of the native LLM process.
  See "The session executable".
- **the claude argv** — claude's harness themed with the session's bro (prompt, spells, MCP namespaces), confined to the claude argv builder plus the harness's private `ScopeRecipe` (the secret manifest), which the outer consumes through the seam.
  See "The claude argv".

A neutral session-shaping flag lands once in the outer and reaches both execution modes and every harness;
a claude-shaping one lands once in the runner or the argv builder and applies to both isolations by construction.

## Per-project defaults (`[tool.bro]`)

Only `--repo PATH|URL` attaches a repository to `ride solo|along`;
cwd is never an implicit attachment.
The checkout's working-tree `pyproject.toml`, or the URL attachment's `pyproject.toml` at its resolved base commit, must carry a `[tool.bro]` table declaring its session defaults (`bro/workspace/project.py`).
Detached launches read no project file:

```toml
[tool.bro]
default = "foo"                      # the bro utilities such as dive-in and scope default to
harness = "claude"                   # optional ride default; claude when omitted
summon-harness = "bro"               # optional: the harness a summon naming none runs its
                                     # child under; bro when omitted
summon-depth = 4                      # optional deepest summon generation
grant = ["github", "@reviewer", ":bro.party.join"]
revoke = [":bro.party.start.boxed"]
image-repository = "custom-images"   # optional: docker repository for the repo's session-container
                                     # images, defaulting to bro/<default> (bro/foo here)
build-context-command = "list-files"  # optional: stdout is the session image's context file list,
                                      # replacing the default `git ls-files`
```

A `[tool.bro.<name>]` sub-table is carried verbatim and interpreted by whoever declares it
— the launch layer validates that its own keys are known and reads no further:

```toml
[tool.bro.analyst]
reports = "docs/analyses"            # repo-relative directory the analyst commits its analyses into

[tool.bro.llm]
sharp = "openai:sol:max"             # a `--llm` preset name and the recipe it stands for
```

`default` is required and names the **project default bro** used by utilities such as `dive-in` and attached `ride scope`;
the mode verbs still require their bro positional.
`harness` is optional (`claude` when omitted) and selects `ride`'s default driver.
`summon-harness` is optional (`bro` when omitted) and selects the harness a summon that names none runs its child under;
a detached launch reads no project file, so its summons use that default.
`summon-depth` is an optional positive integer with no imposed ceiling, setting the deepest summon generation with the root at depth 0 and a default of 2.
The host's `~/.bro.json` value overrides it for the launch, and detached launches use only that host value or the default because they read no project file.
`grant` and `revoke` are optional lists in the full unified grammar.
A project may grant bare credential kinds, `@bro` targets, and `:permit` leaves;
it may not name a credential instance, because the repository does not choose host material.
`image-repository` and `build-context-command` are optional.
A URL attachment evaluates a build-context command in a temporary extraction of the committed base tree and reads the named files back from that commit.
`[tool.bro.llm]` names the repo's `--llm` presets, which the host's own `~/.bro.json` `llm` table overrides per name.
A bro's default recipe on a project is likewise the host's to set:
a `projects.<identity>.bros.<bro>.llm` entry fills what the launch flags leave unnamed, for ride launches and summons alike (`bro/setup/AGENTS.md`, "Host config").
`[tool.bro.analyst] reports` is what an analyst session resolves its output directory from.
A missing pyproject, table, or default
— or an unknown key
— fails the launch.
Which credential instance backs a kind stays out of the repo:
the attachment — checkout path or normalized URL
— keys the host's project selection in `~/.bro.json` (`bro/setup/AGENTS.md`, "Host config"), and `--grant`/`--revoke` overrides it per launch.
A repository may provide `setup.sh` to provision its workspace clone;
the launch logs and skips that step when it is absent.
The project environment need not install `bro-ride`, because session machinery comes from the runtime bundle.
Personas remain registered through the `bro` entry-point group (`bro/reference/extending.md`, "Registering a bro")
— in the invoking installation, whose registry a repository names its default bro out of but contributes nothing to.
`dive-in` is deliberately the one cwd-bound launcher:
it refuses a cwd outside git, resolves that checkout, and passes it to `ride along --repo` explicitly.

## The outer layer

Boxed isolation is the default, and `--boxed` spells it explicitly.
`--unboxed` runs the same clone directly on the launcher's filesystem.
Whatever the isolation and harness, the outer:

- validates policy once — the harness's auth precondition (`preflight_auth`:
  the `claude_code` setup-token for a Claude session;
  the bro harness preflights nothing — its LLM key rides the scoped store) is a launch preflight, so `ride resume` is gated like the launch that created the session.
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
  — the harness's cheap existence check with its own refusal wording (`session_exists` / `missing_session_error`:
  a claude transcript under the workspace's state dir, a bro trail pointer), run before the tree is materialized for a mistyped name (the claude runner resolves the actual session id later, from its cwd);
- calls the shared started-party launcher to prepare the workspace (the two isolation sections below) and the `do-ride` command with only the session shape;
  the launcher emits a container description for boxed isolation or a closed process-environment snapshot for unboxed isolation,
  whose `PWD` names the session tree and whose only ambient inputs are the baseline roster in `ride.runtime_bundle` and, for a session attached to the launcher's terminal, its terminal identity;
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
With an attachment, the first launch creates the same independent clone a boxed workspace uses and checks out the workspace's recorded branch at the resolved base.
Later launches preserve that clone exactly as the session left it.
After clone preparation, `ride` runs the tree's `setup.sh` when present and starts the runtime bundle's absolute `do-ride` with the tree as cwd.
Every unboxed session's store and install-hook output live under one private temporary root that its supervisor removes after exit, so a retained workspace contains no credential material.
`BRO_STORE` and `BRO_INSTALL_DIR` point at those directories.
The harness's `prepare_unboxed_env` hook supplies launch-time state;
`do-ride` installs credential hooks before starting the selected harness.
The launch removes the invoking environment's active-venv marker and PATH entry, so the workspace's own `.venv` is never activated implicitly.

A detached unboxed launch normally creates a plain empty `tree/`, skips repository and persona provisioning, and runs the same pinned `do-ride` there.
With `--tree PATH`, it runs instead in that existing directory while keeping every record under the runtime root.
The auth and scoped-credential preflights happen before a workspace or clone is created.
On a clean dropped exit, the workspace records are removed directly;
an external tree is never removed.
#### Unboxed Claude-state isolation

An unboxed Claude session points `CLAUDE_CONFIG_DIR` at the workspace's `claude/` directory.
The directory carries only the constructed session settings, the setup-token auth, the workspace tree's trust entry, the session plugin seed, and the launcher's account identity when its host state provides one.
The launcher's settings, hooks, permissions, custom agents, and OAuth credentials file do not enter it.
The bypass-permissions acceptance stays interactive because an unboxed session has no container boundary.

### Boxed isolation (`ride along --boxed -w <name> <bro>` — the default)

`/workspace` is always the workspace's writable `tree/` bind.
It starts and stays empty for a detached launch.
With an attachment, the launcher creates a **plain local clone** before container creation.
The same clone machinery serves both isolations and leaves no dependency on the attached checkout's git directory.
A local clone hardlinks objects when the attachment and runtime root share a filesystem and copies them otherwise;
it carries no alternates dependency on the attachment.
The attachment itself is not mounted into the container.
Layout:

- `<runtime-root>/workspaces/<name>/tree/` (host) → `/workspace` rw.
  Empty while detached;
  an attached workspace contains the host-prepared clone.
- `ride-runtime-<bundle-hash>` → `/var/ride/runtime` ro.
  Its `bin/` is first on PATH and its `venv/` carries the root's frozen Python installation.
- a per-launch **scoped credential store** injected into `/home/ride/.bro`.
  Before the container starts, the host resolves only the secrets the session actually uses into an **in-memory** tar and `docker cp`s it into the created-but-unstarted container
  — there is no host-side store and no bind mount.
  It carries one convention-named file per resolved kind plus typed-source annotations in `creds.json`, while the code registry stays in the frozen runtime bundle, so the store directory bounds the in-container resolver to the scoped set
  — any other secret resolves to a clean `SecretNotFound`.
  Hydration is **strict** — a missing secret raises on the launcher before the container is created.
  Living in the container's own writable layer, the store dies with the container:
  `--rm` removes it on normal exit, and an orphaned container (a killed `ride`) is reclaimed by `ride clean`'s container GC
  — secret cleanup piggybacks on the container lifecycle, so no host directory ever holds plaintext and no exit-sweep or signal handlers are needed.
  See "Scoped credential hydration" below.
- **github** and **aws** are ordinary scoped secrets
  — no out-of-band `/run/secrets/github_token` mount, no `~/.aws` mount.
  Each carries a static **install hook** in the registry, applied generically by `credentials install-hooks` (see "Scoped credential hydration"):
  `github` → the git configuration the session carries in its own environment
  — a credential helper over a reset of whatever helper a config outside the session declares, plus the rewrite that carries github ssh remotes to it
  — and a PATH-front `gh` wrapper,
  each resolving the token via `credentials get` per operation (a `github_app`-backed instance mints short-lived tokens, so consumers read at use time rather than off a value baked in at install,
  and `GH_TOKEN` / `GITHUB_TOKEN` inherited from the launching shell are blanked so nothing in the session acts on another identity);
  `aws` → the shared-credentials file it points the CLI at.
  No per-secret logic lives in the entrypoint.
- the host's `/var/run/docker.sock` is **never** mounted:
  its API is root on the launcher with no per-caller authorization, so a socket grant would step past every scoped boundary above.
  Work that needs a daemon
  — building and pushing the operated project's images, say
  — goes through the project's CI instead.
- `<runtime-root>/workspaces/<name>/party/` (host) → `/var/ride/party` rw.
  The root under which joined members' records are created later ("Workspaces"), mounted whole at creation so a join needs no mount of its own.
- `<runtime-root>/trails` (host) → `/var/ride/trails` rw when the scoped trails backend is local.
  In-container readers resolve that fixed absolute path;
  no state path is relative to `/workspace`.

On an attached workspace's first launch, `prepare_container` clones from the checkout or managed mirror into a temporary sibling and publishes the completed clone atomically.
It retargets `origin` to the attachment's upstream URL, converting `git@github.com:` to `https://github.com/`, and ref-refreshes the attachment's `refs/remotes/origin/*` without adding another remote.
It creates the recorded workspace branch from the resolved base:
a URL attachment's fresh `origin/HEAD`, an explicit `--into`, or a summon's inherited base;
a path attachment defaults to its current `HEAD`.
Initialized checkout submodules are cloned from their matching host paths and retargeted to their upstreams;
a managed bare mirror initializes them from their committed URLs, while an uninitialized checkout submodule is skipped.
No prepared clone or submodule carries an alternates file.
Later launches preserve the clone exactly as the session left it.

Inside the container, the entrypoint (running as root first):

1. Aligns the `ride` user's UID/GID with whoever owns `/workspace` on the launcher,
   then re-execs as `ride` (skipped on Docker for Mac when the bind mount reports root-owned via virtiofs — remapping to UID 0 would make claude refuse `--dangerously-skip-permissions`).
2. When attached, marks `/workspace` as a safe git directory.
   The host's `~/.gitconfig` is not seeded:
   a session's git configuration is what its own hooks declare later in `do-ride`.
3. When attached and the optional project image carries `/opt/project-venv`, links it at `/workspace/.venv` and exports `RIDE_VENV_MANIFEST=/opt/project-venv-manifest`.
   A dangling symlink left by an older image is replaced;
   a real existing workspace environment is preserved.
   The repository's `setup.sh` owns manifest reuse and resync from there.
4. When attached, runs `setup.sh` when present, or logs that project provisioning was skipped.
   The workspace venv is not activated:
   PATH remains the pinned runtime shim farm plus system paths.
5. Execs `do-ride`.
   Credential hooks, the plugin seed, and the optional session broxy are per-session work owned by that executable rather than this per-workspace entrypoint.

Every root and spawned child goes through `ride.session.started_party_launch` with a `SessionSpec`, workspace, hydrated scope, runtime bundle, and requested isolation.
The boxed result is one broker-free `ride.workspace.docker.Launch` carrying the full launch:
the workspace name, optional resolved repository attachment and base ref, command, explicit env snapshot, credential tiers, TTY, extra mounts, resolved image tag, and runtime bundle hash.
`prepare_container` consumes that immutable description for clone preparation → scoped-store build → `docker create` + store copy;
it does not re-resolve images or bundles.
The unboxed result is a `ProcessLaunch` carrying the absolute snapshot command, cwd, and complete environment;
broker supervision adapts it to `ProcessLaunchSpec` without reading ambient process state.
An unboxed root defers image and volume resolution until its first boxed summon.

Container images are split:

- **Runtime image** (`bro/ride-runtime:<hash>`) — Python-minor-matched Debian, system CLIs, pinned Claude Code, the ride user, plugin seed, entrypoint, and shell helpers.
  Its hash covers those assets, the Claude pin, and Python minor.
  It contains no Python distribution from the ride installation.
- **Project image** (`<[tool.bro] image-repository>:<hash>`)
  — attached launches may add this layer `FROM` the runtime image, with `/opt/project-venv` and the staged uv manifest set.
  Its hash covers the runtime tag and manifests.
  A repository with no `pyproject.toml`/`uv.lock` pair skips this layer and runs the runtime image directly.

`ride.workspace.build_context` streams separate normalized contexts to `docker build -`:
the runtime context contains only runtime assets;
the project context carries tracked project files, its Dockerfile, and the staged manifests under the reserved `.bro-container/` prefix.
Superseded tags are pruned per runtime or project repository;
plain `docker image rm` leaves any image still referenced by a container.

Network is not restricted by design.

When a boxed session exits, the workspace directory stays on disk for the next session, unless `--drop` was passed and the session exited cleanly (in which case `<runtime-root>/workspaces/<name>` — the tree and every record with it — is removed).

#### Ctrl+Z: suspend and resume

Inside the container no job-control shell sits above the session (`docker-init` is the session leader), so a raw Ctrl+Z byte reaching the container pty would stop claude's foreground group with nothing able to ever resume it
— a wedged terminal.
Every interactive attach therefore binds Ctrl+Z as the docker client's detach key (`--detach-keys=ctrl-z`, replacing the default `ctrl-p,ctrl-q` sequence — so Ctrl-P passes through to claude):
the byte never enters the container, and pressing Ctrl+Z detaches the host-side client instead.

ride tells a detach from a container exit by the container's running state
— the client exits 0 either way.
On detach it freezes the whole container (`docker pause`, the cgroup freezer) and stops its own process group, so the launching shell reports the job stopped exactly like an unboxed Ctrl+Z;
`fg` resumes ride, which thaws the container and re-attaches.
The entire session — claude, the MCP server, the session daemons
— is frozen while suspended.
Both attach paths behave this way (the broker-supervised root and the broker-less fallback);
unboxed isolation needs none of it
— a real shell with job control sits above the session there, so plain job-control suspend already works.
With no job-control shell above ride itself (an orphaned process group), the kernel discards the self-stop and Ctrl+Z degrades to a brief pause + re-attach instead of a wedge.

#### Container credential isolation

The container does **not** bind-mount `~/.claude.json` from the host, nor does the host's OAuth credentials file ever enter the container.
Instead, the launch provisions a container-private `.claude.json` in the workspace's `claude/` dir, which reaches claude inside the mounted dir that `CLAUDE_CONFIG_DIR` names (`ride/ride/claude/claude_config.py:container_claude_state`
— claude state is `ride along` launch data on the neutral `Launch`, so a bro-harness container, which runs no Claude, mounts no claude state at all), while session auth comes from the `claude_code` token (below):

- `claude/.claude.json` — constructed once per workspace from an explicit config (`installMethod: global` to match the image's `npm i -g` install, `hasCompletedOnboarding`,
  `projects["/workspace"].hasTrustDialogAccepted: true` so guided sessions skip the folder-trust prompt,
  and `officialMarketplaceAutoInstallAttempted: true` and `officialMarketplaceAutoInstalled: true` so claude doesn't re-fetch the official plugin marketplace at startup
  — it's baked into the image) plus the host's account-identity fields (`oauthAccount`, `userID`) so the session starts logged in.
  Host machine state (project paths, trust history, usage counters, feature caches) is **not** copied.
  Missing identity is fatal
  — `ride` aborts asking you to log in on the launcher first.
  Subsequent sessions keep whatever the container last wrote.
  Stops per-project mutations (mcpServers, allowedTools, hasTrustDialogAccepted) from being usable to escalate into the next unboxed session.
- **Session auth (`CLAUDE_CODE_OAUTH_TOKEN`)** — Claude sessions authenticate with this env var, which the **required** `claude_code` secret (a `claude setup-token` long-lived token) exports via its registry install hook.
  Claude Code reads it above any credentials file, and one stable bearer is shared by every session
  — so no OAuth credentials file is mounted or synced, and none of the cross-session refresh-token rotation that forced the periodic `/login`.
  Being required, a missing token fails loudly on the launcher at scoped-store hydration, before the container starts (not as a turn-1 401 inside it).
  Bro-run containers request no token;
  only the Claude path does.
  Unboxed sessions get the same var injected into the claude subprocess env directly (`ride.claude.claude_auth.apply_claude_auth`, applied idempotently by both the outer unboxed launch and the `do-ride` session executable next to claude).
  The outer launch reads it from the launch's scoped store rather than the launcher's ambient credential selection.
  The token is equally required there:
  the launch aborts up front when the selected name doesn't load, since the session's private config dir carries no OAuth file to fall back on (see "Unboxed Claude-state isolation").
  The same transform scrubs inherited `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` from the session env:
  both outrank `CLAUDE_CODE_OAUTH_TOKEN` in claude's credential precedence, so a value leaking in from the launching shell would silently hijack the session's auth.
  Full sessions also carry `CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK`, since claude resolves fast-mode availability from the credentials file they don't have
  — left to guess, it reports fast mode as disabled by an organization.
- `claude/settings.json` — constructed fresh each launch (not mounted from the host), holding only UX prefs (spinner verbs, reduced motion, feedback-survey opt-out),
  an explicit `enabledPlugins` opt-in for the `pyright-lsp` Python language server (the host plugin set no longer leaks in, so the container enables it itself),
  a `cleanupPeriodDays` pin keeping transcripts forever (they back the session recording), an `autoMemoryEnabled: false` opt-out of claude's default-on auto-memory,
  an `env` block turning claude's auto-updater off (`DISABLE_AUTOUPDATER`, so no session replaces the claude install it runs — the image's here, the user's own on host),
  and `skipDangerousModePermissionPrompt: true`
  — the workspace is an isolated clone, so the `--dangerously-skip-permissions` acceptance dialog is pre-answered (boxed sessions only; an unboxed clone keeps the dialog).
  The plugin is *installed* at image-build time (`ride/ride/setup/container/Dockerfile`) and staged at `/opt/claude-plugins-seed`, which `do-ride` copies into the bind-mounted `~/.claude/plugins` on first run
  — enabling alone isn't enough, claude would otherwise prompt the "LSP Plugin Recommendation" on `.py` files.
  Host permissions, hooks, plugins, and model/effort pins do not leak in, and the session's own LLM flags plus the merged `--settings` (see "The claude argv") own session config.
- `<runtime-root>/workspaces/<name>/claude/` (host) → `/home/ride/.claude` (container).
  Per-workspace overlay of everything else.
  It is mounted from the host rather than living in the container because the container is `--rm`'d at exit while the workspace outlives it:
  the transcripts are what `ride resume` continues and what `ride list` reads a subject from, and summon control reads the trail pointer beside them on the launcher while the session runs.
- `claude/projects/-workspace/` — where Claude Code stores the session JSONL for `/workspace`.
  The encoded path `-workspace` is fixed (just `/` replaced with `-`).

This means each boxed session has its own private `~/.claude.json` (so MCP server allow-lists are per-workspace) and its own session log directory, while authenticating with the shared,
non-rotating `claude_code` token (so no session's refresh can blow away another's).

#### Scoped credential hydration

Both workspace isolations hydrate only the credential kinds the selected bro and harness declare.
The launch keeps the required and optional tiers kind-addressed and carries a separate kind-to-instance selection.
The resulting store directory is the boundary:
every unboxed session materializes it under a private temporary root owned by its supervisor;
each points `BRO_STORE` at its own store, and its install-hook output stays under the same temporary root.
A boxed launch injects it at `/home/ride/.bro` and explicitly sets `BRO_STORE=/home/ride/.bro`.
An in-session resolver therefore never consults the host's ambient selection.
Unboxed scoping is still a convenience rather than a security boundary, because the session runs as the host user.

- **The needs.**
  A bro's `needed_secrets()` (`bro/bro.py`) is the union of each selected MCP server spec's and data source's `needed_secrets`, the bro's MRO-collected `extra_secrets`, and the credentials of its pinned-on features.
  It deliberately omits the LLM key, which only surfaces running the bro as an LLM process add.
  `optional_secrets()` supplies the bro's optional needs, and a launch that records adds `trails` as another optional need.
  A held kind is optional exactly when at least one source needs it and every source that needs it marks it optional.
  A grant therefore does not promote an optional need, while a kind held only by a grant is required.
  Components and manifests declare bare kinds only.
- **Which instance.**
  Scope starts with the repository's `[tool.bro]` layer, then applies the host config's `defaults`, matching project URL, project path, URL-bro, and path-bro layers, and finally the launch flags.
  Each layer's `creds` or `--cred` picks an instance without adding its kind;
  a held kind with no pick reads its empty instance.
  A bro layer overrides the project selection for that bro, including when the bro is a summon target.
  A bro-layer pick of a kind its configured scope does not hold fails and names `grant`;
  recording's `trails` need counts for this check even under `--no-trails`.
  A launch `--cred` pick whose kind is absent after the launch's own grant/revoke changes fails and names `--grant`.
  The bro's declarations are evaluated under the final selection, so a feature gate resolves on the launcher exactly when it resolves in the session.
  Every credential name in every applicable configuration layer must be registered by the installation.
  An unregistered name fails the launch with its layer;
  an unregistered `defaults` name points to the project entries as its narrower replacement.
  A path attachment matches on two identities
  — the checkout path and its `origin` URL
  — so a `projects` key written as the repository's URL reaches the everyday `--repo <path>` launch too;
  within each of the two ranks the path entry layers over the URL entry.
  A detached launch or an attachment with no matching project entry falls through the layers that do apply to the kind's own stored material.
  `ride scope` prints each declared kind's stored name, the layer that picked it (or `unpicked` for the empty instance), and its `PRESENT`, `SKIPPED`, or `MISSING` state without loading it.
- **Which bro.**
  `ride solo|along` computes the scope for the mode verb's bro positional.
  Summon lowering computes it for the child target, so the child's own project-bro layer applies.
  A bro name the registry does not resolve fails before workspace or container creation.
  Direct `bro run` and `bro chat` use ambient credentials and do not call this layer.
- **Per-recipe sets.**
  Harness implementations own `ScopeRecipe` values and pass them to the shared scope computation.
  Claude uses the persona's claude-harness manifest plus `claude_code`;
  the bro harness uses the full manifest plus the resolved LLM recipe's key.
  Each surface includes the bro's matching optional needs.
- **Launch overrides.**
  `--cred KIND+INSTANCE` replaces the computed pick for a held kind.
  `--grant KIND` adds the kind, and `--revoke KIND` removes it.
  Credential grants and revokes use the same forgiving fold in configuration, a launch, and a resume:
  the last layer wins, restating either state is harmless, and revoking an absent kind does nothing.
  One layer cannot both grant and revoke a credential kind, and credential grants and revokes cannot name instances.
  `@bro` values adjust the summon allow-list, and `:permit` leaves adjust party authority instead;
  their launch overrides retain strict no-op checks.
  On resume, each supplied credential grant, revoke, or pick replaces the recorded value for that kind, and `--revoke KIND` also drops its recorded pick.
- **Hydration.**
  `credentials.build_scoped_store(store, required, optional=…)` returns an in-memory file map plus the declared kinds that loaded.
  A name is present when `Store.instance_names` finds either its convention material file or its `creds.json` entry, without resolving the source.
  An optional kind is skipped only when no layer picked it and its empty instance is absent.
  Every other held name must be present and load successfully;
  a missing picked instance, source failure, minting failure, malformed value, or unresolved `$cred` reference fails the launch under the credential's name.
  Whole-store shape validation still runs when the `Store` is constructed.
  Each selected instance materializes under the kind's empty instance, `creds/<kind>.cred`, with typed-source annotations in `creds.json`, so the scoped namespace stays kind-addressed.
  A reference-preserving `$cred` chain hydrates its referenced kinds transitively, but those transitive targets are not reported as declared hydrated kinds.
  Instance-spelled references in reference-preserving material fail because the scoped namespace is kinds-only.
- **Install hooks.**
  Hooks come from the frozen code registry and apply only for the declared hydrated-kind list returned by the build.
  Both launch isolations pass that list through `BRO_INSTALL_KINDS` beside `BRO_STORE`.
  `do-ride` applies it from the same frozen runtime bundle into `BRO_INSTALL_DIR` when one is supplied, otherwise into the isolation's session environment directory, and exports the returned wiring before the harness starts.
  A listed kind missing from the scoped store fails;
  a listed kind with no hook is a no-op.
  Transitively hydrated `$cred` targets are deliberately absent from the list, so a shipped reference never wires tools for its target.
  The hook directory is recreated per launch, and two hooks contending for one file, variable, or command fail instead of silently ordering.
- **The container contract.**
  The scoped store is `docker cp`'d into the created-but-unstarted container and lives only in its writable layer.
  Its files land mode `0600`, and the entrypoint re-owns the store after remapping the `ride` user's UID/GID.

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
The root launch carries the session's summon allow-list (`run_root_via_broker(may_summon=…)`, computed at launch by `ride/ride/bro_worker.py`;
see the shared launch flags above), while `LaunchControl` enforces common launch arguments and `BroType` enforces per-peer summon authorization (see "Summoning another bro").
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
its optional noVNC loopback view requires `:webview.vnc`.
Its declaration ships a byte-valued Docker build context whose normalized relative paths include a `Dockerfile` opening with `ARG RUNTIME_IMAGE` and `FROM ${RUNTIME_IMAGE}`, plus a command, environment, and distinct container ports.
The environment cannot claim host-owned `BROKER_*`, `RIDE_*`, `BRO_*`, `HOME`, or `PATH` names.
The runtime image tag and sorted build-context files determine the worker image tag `bro/<type>:<hash>`;
ride builds a missing tag lazily under a per-tag lock and prunes unused predecessors from that type's image repository.
A launch reserves its tag from the build through container creation, so concurrent builds of different tags cannot prune an image another launch is about to use.
A failed build reports the captured output tail with its exit status.

The host lowers each run off the broker loop into a detached throwaway boxed workspace named `<type>-<channel>`.
It mounts the frozen runtime volume read-only, an empty scoped credential store, and the worker's read-only artifact view at the absolute normalized POSIX path the declaration names, linking every accepted `share` ref before start.
The view path defaults to `CONTAINER_ARTIFACTS_ROOT` and is a launch fact, so changing it does not rebuild the image.
It mounts no session, party, or trails state.
The declared command runs as `broxy run -- <command…>`, so its clients multiplex through the worker's one upstream attach.

For each declared container port, the host reserves an available launcher-loopback port, releases the probe, and creates the container with `-p 127.0.0.1:<host>:<container>`.
A process taking that host port before `docker start` makes the launch fail rather than moving the binding.
The worker receives the selected mapping as `RIDE_PUBLISHED_PORTS=<container>=<host>,…`, reports any usable address to its owner over its granted talk, and exposes the same host/container pairs through peer facts and the launch audit.
Docker supervision captures a bounded merged output tail;
a clean exit removes the throwaway workspace, while a failed or killed worker keeps it and includes the tail in the synthesized death result.

### Summoning another bro

A session can summon another bro over its channel.
The summon surfaces are wrappers over `launch {type: bro, …}`, the same request kind every registered worker type uses.
The target runs as a one-shot, non-TTY session that either starts a party of its own or joins the summoner’s party.
A *manual* summon instead has the user launch the child themselves either interactively or one-shot;
see "Manual summon" below.
The child's credentials come from its own bro, harness, model, and project/host configuration, as for a root launch with no credential flags.
A request carrying a credential name in `grant` or `revoke` is refused and points to `projects.<identity>.bros.<bro>` in the host config.
The child's summon allow-list and permits come from its own seeds and project/host layers under the request's `@bro` and `:permit` overrides, never by inheriting the summoner's sets.
The root launch's `--env` additions are the one input every started child and joined member does inherit, as facts about the environment the party runs in.
An explicit request grant is bounded by the corresponding target or permit the summoner holds.
It runs under the harness the request names, or the launch's `[tool.bro] summon-harness` when it names none.
Both harnesses run `do-ride solo …`:
`bro` spawns the target's own LLM process there, while `claude` starts a one-shot managed Claude Code session of the target persona.
The request’s `party` field accepts `start` or `join`, and a start’s optional `isolation` is `boxed` or `unboxed`.
The CLI spells those choices as `--start`, `--join`, `--boxed`, and `--unboxed`.
An unmarked request starts boxed when the summoner holds `:bro.party.start.boxed`, otherwise unboxed when it holds `:bro.party.start.unboxed`, and otherwise fails naming the permits held;
it is never converted into a join.
An explicitly boxed or unboxed start requires the matching start permit.
A join is always explicit, requires `:bro.party.join`, inherits the summoner’s party isolation, and refuses `isolation`, `into`, and `manual`.
The started-party lowering emits `DockerLaunchSpec` or `ProcessLaunchSpec` through the common launcher roots use.
An unboxed join emits a member `ProcessLaunchSpec` in the summoner’s existing tree with an explicit environment snapshot and loopback broker upstream.
A boxed join emits a member `ExecLaunchSpec`:
a `docker exec -i -u ride -w /workspace` client into the party’s running container, running `env -i` with an explicit baseline (`HOME`, `PATH`, `TERM`, `LANG`) plus the member env
— an exec otherwise inherits the container’s config env, the party’s first session’s entire launch environment
— with `BROKER_UPSTREAM` under `host.docker.internal` and the member’s records reached through the party mount (`RIDE_SESSION_DIR=/var/ride/party/<member>/session`, `CLAUDE_CONFIG_DIR=/var/ride/party/<member>/claude`).
The member’s scoped store is `docker cp`’d into the running container at a per-member path under `/home/ride/.bro-party/` and re-owned the way the entrypoint re-owns the root’s.
The exec client’s death does not end the process inside, so the exec’d command records the member’s pid and start time under its session dir before it execs `do-ride`;
the handle treats that record — or the client’s exit — as the started handshake, and `do-ride` removes the record at exit.
An unboxed child starts in its own process group;
kill sends SIGTERM only to `do-ride` so its harness-specific shutdown can unwind and flush state, then sends SIGKILL to the group if the process tree or inherited output pipe survives that grace period.
A boxed member’s kill follows the same TERM → grace → KILL shape through `docker exec`, signaling only while the container’s `/proc` still shows the recorded start time, so a late kill finds no target and never a reused pid.
The request’s `llm` recipe, settled over the host’s per-bro entry like a launch’s own, resolves within the child’s harness and never switches it;
the child runs with the root session’s attachment.
A started child in an attached ride bases on the summoner’s workspace `HEAD` read at summon time (uncommitted changes never transfer;
a container summoner’s local-only commits are transferred into the attachment first so the child’s host-side clone can copy them) unless the request’s `into` ref overrides, while a detached root starts detached children and rejects `into`.
A joined child uses the party’s existing tree directly, including its current uncommitted state, and runs no workspace setup or persona provisioning of its own.
The quest carries a fixed **talk** over four rights:
`owner.say`, `owner.question`, `worker.say`, and `worker.question`.
A reply is authorized by the other end's question right.
A summon defaults to `worker.say` and widens only through the request's `talk` field, spelled `--talk <right>[,<right>]` by the CLI;
the host fixes and enforces the set when the quest opens, while both peer surfaces refuse a forbidden move before sending.
The answer comes back synchronously, while permitted messages can travel for the quest's whole life.
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
  The forwarded fields are `--timeout <s>` / `--into <ref>` / `--hold <level>` / `--grant <name>` / `--revoke <name>` / `--share <ref>` / `--talk <right>` / `--harness <name>` plus the LLM flags;
  an omitted hold leaves the child's unattended default.
  `--share` hands the child read access to an artifact ref — see "Sharing artifacts between peers".
  Grant/revoke shape the child's onward authority with `@bro` and `:permit` values only.
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
  `--wait` blocks for the reply and prints it, exiting 4 with the id when its optional bound passes first, the reply still recoverable from the journal.
  Concurrent waiters and later reads see the same state.
  `quest list` walks the caller-scoped paginated `query {}` listing and prints retained bro summons live-first, including their talk and pending questions.
  `quest watch` first takes the current `events {}` head, then replays retained own-quest messages and open questions whose journal sequence is no newer than that head, marked `before the watch`.
  It then long-polls ordered bro-quest lifecycle and chat events after the head, so chat committed between the head and replay queries arrives once through the stream.
  On a child quest it names the target and quest;
  on the session's own quest it renders the other end as `summoner` and never echoes the session's own says.
  An event-retention gap prints a notice, re-arms from the current head, and repeats the retained replay.
  `quest cancel <id> [--timeout <seconds>]` ends a bro quest this session owns and waits for it to end;
  it exits 0 once the quest has ended and 3 when the bound passes first, the end still on its way.
  `mission check|history|say|ask|share|list|watch|cancel` is the corresponding universal surface for every worker type:
  Chat payloads are JSON objects, and `history --seq N` recovers one full retained entry.
  `share <id> <ref> [--timeout <seconds>]` hands an owner-reachable artifact to a live worker.
  List and watch accept `--type`, and watch cuts chat lines over 1 KiB with a history pointer.
  In a claude session, long summons run via the harness's background Bash;
  `rewind show <trail-id>` peeks mid-run.
  Contract details in `bro/summon.py`, `bro/mission.py`, and `bro/quest.py`.
- the bro service tools (`bro::summon`, then `bro::quest_check` / `quest_history` / `quest_say` / `quest_ask` / `quest_list` / `quest_cancel` on the `quest_id` it returns), for bro LLM processes, and mounted beside the CLIs in a claude session.
  On the bro harness, `summon` returns the accepted state after host acceptance and has no `detach` parameter;
  answers, questions, replies, refusals, and terminal states arrive through `quest watch`.
  `quest_say` sends and returns, `quest_ask` mints a question id whose reply arrives through the watch;
  `quest_check` and `quest_history` are one non-blocking journal read each;
  and `quest_cancel` returns once the host accepts the cancellation, with the terminal following on the watch.
  `quest_list` is the same paginated journal listing on both harnesses.
  On the claude harness, the blocking service tools retain their polling controls:
  `summon` blocks unless `detach: true`, `quest_ask` may wait for the reply, `quest_check(wait=true)` long-polls to the end or a child question, `quest_history(wait=true)` to the next message, and `quest_cancel` may wait for the terminal.
  Each MCP blocking call owns its channel client so cancellation aborts the current short wait, while the host journal retains the outcome and chat;
  its transport cautions keep waits under the harness cap and recover by id instead of sending twice.

A claude-harness child is scoped through the claude recipe
— `claude_code` required, no LLM key
— and the seam's auth preflight runs in the lowering, so an unresolvable setup token fails the spawn with the preflight's remedy as the correlated launch failure.
Its lifecycle comes from the `do-ride` session executable rather than `bro.native.runner.Runner.run`:
the runner takes the last turn's reply, announces the trail mark once the session recorder publishes the workspace's current-trail pointer, and sends the quest's ok result on a clean exit
— a non-zero exit emits no result and surfaces as the synthesized `result{failed, reason: exit}` (the echoed reply lands in its output tail), while an unattended abort is the `raise` service tool's own `result{failed, reason: raised}`.
The recorder stamps the child trail's `summoned_by` from the summoner attribution, and the child's recorded resume spec is a claude spec, so a kept workspace resumes into the claude conversation.

### Manual summon — a child the user launches

A `manual: true` summon (`summon --manual`, or the `summon` tool's `manual` parameter, which never blocks for the answer) inverts the launch.
It takes the same `talk` / `--talk` widening as a spawned child, so the interactive session can exchange only the roles fixed at registration.
The summoner needs either party-start permit, but the request refuses `party` and `isolation` because the user's launch owns the actual placement.
The host spawns nothing and instead registers an *expected external peer*
— a provisioned broker channel awaiting a child someone else starts
— and the quest id doubles as the launch token.
The registration is acknowledged with an `accepted` mark once the token is claimable, and the manual client waits for it, so a denial fails at the summon itself
— a token is only ever handed out for a summon the launcher is expecting.
The summoner relays `<runtime>/venv/bin/ride along --summoned <token> <target>` as the default launch command for an interactive session, using the ride's own host runtime.
For a one-shot request, the user can instead run `<runtime>/venv/bin/ride solo --summoned <token> <target>` and leave the run unattended.
Both are otherwise normal launches
— boxed or unboxed, either harness, the user's own `--llm`/`--hold`/`--workspace`/`--cred` and credential `--grant`/`--revoke`
— except they start no broker of their own.
The omitted hold follows the selected mode's default:
`along` is attended when boxed and guided when unboxed, while `solo` is unattended.
The launcher puts the summoner's provisioned channel in `BROKER_UPSTREAM`, and the session broxy publishes the local `BROKER_CHANNEL` its processes use to attach as a regular summon peer.
Its own nested summons therefore route through the summoner's control with per-peer authorization.
The request fixes what the summoner authorized
— the target bro, the prompt (delivered as the session's first message), the root session's repository attachment, the base (the request's `into` ref, or the summoner's workspace HEAD read at launch, like a spawned child's at its spawn),
the child's resolved `may_summon`, permits, and quest talk, and the party's `--env` additions.
The pending record carries no credential seeds.
The launch's own `--cred` and credential `--grant`/`--revoke` layer shapes its material, while `@bro` and `:permit` overrides are refused because the control enforces the sets it resolved at request time.
`--env` is refused as well:
the control stamps the party's additions on every summon the child makes, so the child carries exactly those.
Launch-owned request fields (`timeout`/`hold`/`llm`/`harness`/`party`/`isolation`) are refused at the request:
the human at the launch owns the session's shape, and there is no host-killable child for a timeout to bound, so a manual summon carries no timer at all.

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
The child announces the trail mark (`{trail_id}`)
— the Claude runner from its trail watch, the native chat surface on its first turn.
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
the token works with `quest check` / `quest list` / `quest watch`, reading as running until the user launches.

Host side, `PeerFacts` (`ride/ride/peer_facts.py`) holds a generic `WorkerFacts` row keyed by the mission a peer undertakes:
type, workspace and optional joined-member name, permits, expected/artifact-view state, published ports, and a type-owned extension.
The bro extension carries its name, effective allow-list, and resolved placement.
The journal's host-anchored mission seeds the root row;
an authorized launch adds its worker row before starting, with a started bro’s channel-named workspace, a joined bro’s inherited workspace plus channel-named member, or the claimed workspace for an expected bro filled at launch.
Every owner resolves through one join
— peer to undertaken mission through the dispatcher's worker binding, then mission to facts row
— and depth is the journal ancestry length.
`LaunchControl` (`ride/ride/launch_control.py`) validates the common request and `BroType` (`ride/ride/bro_worker.py`) authorizes the summon against that row's extension.
A child's allow-list and permit set are its static seeds under the project and host configuration layers, then its request's matching `@bro` and `:permit` values.
The configured layers are idempotent;
a malformed or no-op request override is denied outright.
Summons chain transitively wherever the seeds chain, and widening is always explicit and bounded by the summoner:
its own list never passes through
— only what its request names
— and it may only name bros it is itself allowed to summon, so authority only narrows down a chain.
Credential names in a request's grant or revoke list are denied at the request boundary with the target bro's host-config entry as the remedy.
The lowering computes the child's scope from that bro, the requested harness and model, and the applicable configuration without launch credential flags.
The summoner's credential scope therefore never enters peer facts or authorization.
Resolving the harness and LLM pair on the loop still settles the recipe:
one the named harness cannot run is denied at the request rather than failing the spawn.
A peer the control cannot attribute a bro to is denied, and the launch-resolved depth cap guards against seed cycles recursing through real containers.
The root sits at depth 0, and a request that would create a child past the configured `summon-depth` is denied.
Denials reply immediately and land in the journal and audit as `denied` transitions (reason, quest id, summoner, and bounded request args).
Each spawned child records `summoned_by` provenance from the summoner's current trail plus the summoning bro's own `tool_call` step id when the request carries one.
Owner attribution has one shape in the audit: `{workspace, type, member?, bro?, trail_id?}`.
The trail is read from that session’s pointer for every request because Claude segments move it
— the workspace’s `session/` for its first member, or `party/<member>/session/` for a joined one
— with the answered quest’s journal `trail` mark as fallback.
The authorized spawn carries its `SummonSpawner` into the dispatcher with the requesting peer as parent.
`SummonSpawner` lowers the request off-loop through the started-party launcher or joined-member builder, then dispatches its concrete Docker, process, or member-exec description;
a grandchild’s lifecycle routes to the child that summoned it, and root exit still tears down the whole tree.
Every event lands a host log line and a durable audit row under `<runtime-root>/launch/<name>.jsonl`.
Each row keys the ride under `ride`, using the root workspace name.
Each entry names its actual `owner`, worker `type`, `published_ports`, bounded request args, transition, trail id, and terminal outcome.
Type-owned audit fields are isolated under `extension`;
the bro contributes its target and resolved `placement` as `{party, isolation}` there.
The placement records the effective isolation inherited by a join, while a manual summon's isolation stays null because the user's launch settles it.
Live readers never read that audit back:
check, list, watch, and the session-local statusLine projector query the caller-scoped in-memory journal over their own broker channel, so the same surfaces work at any summon depth.
Quest verbs keep the bro workflow's type filter, while the mission surface, turn-end guards, and statusLine account for every owned mission.
The statusLine places a child's pending question beside its live summon, renders another live launch as its worker type, and keeps the most recent terminal outcome briefly visible.
The scope is the quests the caller requested and nothing beneath them;
it excludes the parent-owned quest that the caller's own worker answers.
Each authorized launch also carries the allow-list and permits it will be judged against into the run itself (`RIDE_MAY_SUMMON` and `RIDE_PERMITS`:
the session root's at launch, a summoned child's own resolved sets at its spawn), so a peer reads its authority off the banner instead of discovering it by denial;
enforcement stays entirely host-side.
Root exit kills in-flight children with a loud log naming what was killed;
a result lost that way stays recoverable from the child's trail.
A SIGTERM to the launcher ends the ride the same way:
the root run reports the signal's exit code and its teardown kills the tree, the members running in sessions of their own included, rather than the signal orphaning them.
A started-child supervisor removes its throwaway workspace only after a clean exit and retains it after failure or kill for inspection and recovery.
A joined-member supervisor removes `party/<member>/` only after a clean exit and retains it after failure or kill;
the member has no workspace or resume record, so its trail is its recovery surface.
A party ends with its first session:
that session’s teardown kills any members still running, loudly, whichever spawner runs them.
The unboxed process handle always removes the private credential state separately, and a failed removal fails teardown rather than reporting the child complete;
a boxed member’s store lives in the party container’s own layer and dies with it.
Each authorized start records the child’s run as its `broker-<channel>` workspace’s resume record
— the same solo session spec a `ride solo` launch under the child’s harness would record
— so `ride list` shows the child under its prompt and a surviving workspace resumes like any kept solo workspace:
`ride resume broker-<channel>` opens an interactive `bro chat` continuing the child’s trail (see "Bro harness").

### Sharing artifacts between peers

Peers pass files by content-addressed reference through the ride's store the launcher owns (`ride/ride/artifacts.py`; the wire contract, ref grammar, and CLI are `bro/artifact.py`):

- `artifact mint <path>` ingests a file or directory named relative to the minting peer's workspace root
  — a private reflink-or-copy, so nothing the producer writes afterwards changes stored bytes
  — and prints its ref:
  `sha256:` plus the content digest for a file (so `sha256sum` checks it) or the digest of a canonical typed-entry manifest for a directory (`artifact digest <path>` computes either locally, which is how a ref is verified end to end).
  Re-minting unchanged content answers the same ref without storing anything new;
  a mint past the ride's byte cap is refused rather than evicted.
- `artifact get <ref>` makes a ref visible to the requesting peer and prints the path it appears at.
  A boxed peer reads it under its declared artifact-view path
  — `CONTAINER_ARTIFACTS_ROOT` unless its worker-container declaration chooses another absolute normalized POSIX path, as webview does with `/workspace/artifacts`;
  the per-peer view directory is bind-mounted read-only, so a ref shared while the peer runs appears without a remount and writes fail with `EROFS`
  — while an unboxed party, having no mount namespace, gets a private copy under the workspace’s own `artifacts/` directory, shared by its members.
  Either way the path is not for editing in place;
  a peer that wants an editable copy makes one.
- Reach follows the launch tree, and nothing a peer says widens it:
  a mint is readable by the minting peer and its summoners up to the ride root;
  a launch request's `share` list (`summon --share <ref>`, the service tool's `share` field, or another worker type's corresponding launch argument) hands refs the owner itself can read down as the mission opens;
  and `mission share <id> <ref>` gives one such ref to a live mission owned by the caller.
  A share into a boxed worker appears in its mounted artifact view without a remount.
  There is no other path
  — knowing a ref is not access, and a denial is uniform whether or not the ref exists.
  A manual summon refuses launch-time and live `share`, and a manual child's `get` is denied
  — the launcher builds no launch for it, so no view is mounted
  — while its mints flow upward normally, attributed to the workspace its own `--summoned` launch claimed the token with.
- The store is ride-scoped and dies with the ride
  — a resumed root starts a new ride with an empty store, so a stale ref fails at its own `artifact get`
  — while mints, gets, shares, and denials outlive it in a JSONL audit under `<runtime-root>/artifacts/<ride>.jsonl`, beside the launch audit.
  Each audit row keys the ride under `ride`.

Worker types resolve accepted shared refs through `Host.artifacts`, under the same sharing check.

### The launcher↔session contract

The launcher and `do-ride` come from the same frozen or explicitly given runtime, so workspace age and the installation that first received a manual token cannot skew their contract, and the operated repository need not install either command.
`ride clean` sweeps idle workspaces, unreferenced managed mirrors, and unlocked runtime bundles;
removing a bundle also removes its unused runtime volume, while Docker keeps an in-use volume alive.

## The session executable

`do-ride solo|along --workspace NAME --harness H [--resume] [--repo R] --hold HOLD [--llm L] <harness flags> <bro> [prompt] [-- args]` (`ride/ride/do_ride.py`) runs one session in a prepared workspace.
It has its own parser:
there is no `--in-place`, no outer machinery flags or combination refusals, and `--resume` is an ordinary session flag.
Unboxed isolation runs it from the snapshot venv and boxed isolation from the mounted runtime volume;
both expose only pinned session shims plus system paths.
The distribution declares `do-ride` as both a console script and a session command, so every runtime bundle carries it beside `ride`.

`do-ride` receives `RIDE_ISOLATION` and the attached tree's `RIDE_BRANCH` / `RIDE_BASE_SHA`.
It then exports the bro git identity, `RIDE_WORKSPACE`, `RIDE_REPO`, `RIDE_REPO_URL`, `RIDE_BRO`, and the `BRO_HOLD` / `RIDE_RUNNER_PID` pair (see "Forwarded env vars").
It applies the persona's declared workspace provisioning when attached (`BaseBro.provision_workspace`), installs the scoped credential hooks, prepares missing Claude state and the installation's plugin seed, and owns the optional session broxy.
Each step is idempotent, so a launcher may pre-provision state before invoking it.
While the harness runs, `runner.pid` under `RIDE_SESSION_DIR` records the executable's pid and operating-system start-time identity as JSON;
`do-ride` removes only the record it owns when the session exits.
A harness supplies only `run_session`, so nothing every session needs is written once per agent loop.

The Claude harness's runner (`ride/ride/claude/runner.py`) then, in order:
resolves a resume's Claude session id from its cwd's projects dir;
starts the session-local MCP server and surfaces bro spells (both below);
builds the Claude argv (below);
starts the session recorder daemon (see "Session recording");
gates on the server's `/health`;
then runs `claude` and waits, ending it on a SIGTERM aimed at `do-ride` (`docker stop`, kill, a terminating service tool) as the interrupt a user issues rather than as a signal, so the turn in flight reaches the transcript before Claude goes.
`ride/ride/claude/interrupt.py` owns that per-flavor split.
After Claude exits the runner stops the server and recorder (the stop is the recorder's final append and trail end).
The bro harness's runner resolves a resume's trail from the session's current-trail pointer and spawns the native `bro run|chat …` argv.

### The claude argv

The builder (`ride/ride/claude/claude_argv.py:build_claude_launch`) assembles the merged `--settings` (fastMode, the statusLine, the attribution opt-out, and the hooks),
the forwarded claude args, prompt seeding, the `--model` / `--effort` / fastMode it reads off the session's claude-code recipe,
the ride-injected `--append-system-prompt` (see "Auto-injected system prompt"), `--dangerously-skip-permissions` under every `--hold` level but guided,
the `--mcp-config` mounting the persona's namespaces from the session-local server below, and `--disallowed-tools mcp__claude_ai_*` to keep account-level claude.ai MCP integrations out of the managed session.

The statusLine is ride's, not the operated project's.
The runner starts one session-local projector process from its frozen runtime;
it queries the journal, reads recording health, renders the clock-relative line at the statusLine refresh cadence, and atomically writes it under `session/claude/`.
Claude's settings command only checks that projector's pid and cats the projection file, so each event-driven or interval render starts no Python process and performs no broker call.
The projector holds a session-state lock and exits immediately when its parent-owned liveness pipe reaches EOF.
A resumed runner waits for that lock before clearing the prior files and starting its writer, so abrupt host-parent death cannot leave two projectors racing one workspace.
During a live parent, a runner-side monitor waits and reaps the projector, removing only files still owned by its pid;
the pid guard covers that exit-to-cleanup window, so an unexpected child death renders blank rather than stale.
The remaining settings commands use the `do-ride` interpreter, so neither leans on PATH resolution nor on a file mode the wheel format does not guarantee;
flagSettings outranking project settings means a consuming project neither declares a statusLine nor can break the session's with a stale one.
The corollary is that a bare `claude` launched outside ride gets claude's default bar.

Claude's own attribution is off in every managed session
— its commit trailer, its pull-request line, and the appended session link:
the framework carries commit attribution of its own, and the same flagSettings rank keeps a consuming project's settings from turning claude's back on.

### Session-local MCP serving

Every session gets its MCP tools from a session-local HTTP server the runner owns
— one mechanism for both execution modes, dying with the session.
The runner starts the PATH-selected `mcp-server persona:<name> --http` on an OS-assigned port (a fixed port would collide between concurrent sessions sharing a netns) with a per-session bearer token.
`mcp-server --http --port 0 --port-file <path>` binds the socket *before* its heavy imports and publishes the real port through the port file, which the runner polls (milliseconds) before building the `--mcp-config` and launching claude;
a claude connect that lands mid-import sits in the TCP backlog until uvicorn accepts on the pre-bound socket.
The server is terminated when claude exits;
a SIGKILLed runner orphans it.
Its output lands in a `ride-mcp-*` temp dir alongside the port file.

`bro-ride` contributes the `persona:` target prefix through `bro.mcp.targets`;
the core `mcp-server` discovers the matching resolver without knowing the target.
`ride.claude.assembly` resolves `persona:<name>` through `persona_servers()`
— `BaseBro.assemble(harness='claude', ...)`, yielding only the additions that hold on the Claude harness, plus the `spell` and bro service servers;
an entry gated `harness == 'bro'`, like the dev toolset, never mounts, since Claude's built-ins cover it.
`bro::cast` joins the service server when the bro has spells and OpenAI resolves.
Selected `block(...)` layers join `--disallowed-tools`, removing the named Claude-native tools;
selecting a block for the `bro` harness is a declaration error.
Persona sessions rely on Claude's native third-party skill mechanism instead of mounting `bro::skill` or generated spell adapters.
The assembly also mounts the `raise` service tool when the session is unattended (`BRO_HOLD=unattended` + `RIDE_RUNNER_PID` in the server's inherited environment — see "Forwarded env vars"),
in its terminate-the-session flavor (semantics in `bro/AGENTS.md`, "Service tools");
every other level gets no `raise`
— a human exists to report to.
There is one streamable-HTTP endpoint per tool namespace;
the argv builder mounts each endpoint under its namespace as the claude server key, so tools surface as `mcp__<namespace>__<tool>`
— `/tasks` → `mcp__tasks__list_tasks`, `/bro` → `mcp__bro__banner`, `/<name>-source` → `mcp__<name>-source__<tool>` (e.g. `mcp__current-time-source__get_time`)
— matching the `bro/prompts/tool_names.md` convention.
Every server entry in a ride-generated `--mcp-config` carries `alwaysLoad: true`:
a headless (`-p`) run then holds its first request until the server is connected, but an interactive session's argv-seeded first prompt does not wait
— claude's MCP connects stay async, so that first turn can reach the model with no tools attached, bridged by Claude's own still-connecting reminder and ToolSearch.
The runner polls the server's `/health` until ready *before* launching claude, so the configured bro and tool graph's potentially heavy imports is paid off claude's critical path instead of inside that startup block and its connect timeout;
the runner's own argv build overlaps the server's import, so much of the wait is already paid when the gate is reached.

In boxed isolation the server runs inside the container, so the scoped credential store carries the served tools' own secrets (the persona's claude-harness manifest)
— no deployed-server token is involved.

### Bro spells and skills

A bro's spells (the files its `spells` declaration names, `bro/reference/extending.md`, "Declaring a bro") are canonical `spell::<name>` tools;
`bro::cast` joins the service server when OpenAI resolves, and the Spells contract routes `[[…]]` markers to it where it is mounted and to the spell's own tool otherwise.
A session gets the Spells contract through its append prompt and keeps Claude's native third-party skill discovery;
bro spells are not copied into `.claude/skills/` and have no slash-command aliases.


## Auto-injected system prompt

For every `ride solo|along` session, `ride/ride/claude/system_prompt.py:session_append_prompt` builds the `--append-system-prompt` text:
the base prompts from `bro/prompts/shared/*` and the top-level reference docs the loader registers (see `bro/prompts/AGENTS.md` for the inventory), plus the session bro's own persona prompts (`BaseBro.persona` — the selected bro),
so the session carries the bro's policies under Claude's own harness.
`shared/` is also injected into every bro;
the top-level reference docs are Claude-Code-specific and reach no other harness.

The session also carries the hold fragment, rendered at launch by `bro.prompts.hold_fragment` from the session's `--hold` level (the level files live in `bro/prompts/holds/` — see `bro/prompts/AGENTS.md`, "Hold text"),
so a session is told its hold up front rather than detecting it at runtime.
Bro-native runs compose the same level files through `bro/bro.py:BaseBro.system_prompt_for`, from the hold their own launch surface picks (`bro/launch/AGENTS.md`, "Display and holds").

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
  Set by each launcher and overwritten from `do-ride --repo`;
  banner and summon lowering read this launch state rather than deriving a repository from cwd.
- `RIDE_REPO_URL` — the attached session tree's sanitized network `origin`, absent when detached or when `origin` is a local path or `file://` URL.
  Each launcher reads it on the host side, and `do-ride` refreshes it from the prepared tree before the harness starts.
- `RIDE_BRANCH` — the attached workspace's recorded branch, absent when detached.
  The launcher reads it from `workspace.json` and passes it to `do-ride`.
- `RIDE_BASE_SHA` — the commit the attached workspace's tree starts the session at, set beside `RIDE_BRANCH` and absent with it:
  the resolved base a fresh clone is made at, or the tree's HEAD for a resume and for a joined member.
- `RIDE_HOST_WORKSPACE` — launcher-side absolute path to the workspace tree, set explicitly in both isolations.
  It is normally `<runtime-root>/workspaces/<name>/tree`, or the recorded external path for `--tree`;
  in a container it names the host path bound at `/workspace`.
- `RIDE_HOST` — the launcher's hostname, set explicitly in both isolations.
- `RIDE_RUNTIME` — launcher-side absolute path to the ride's materialized host runtime.
  Manual-summon surfaces use its `venv/bin/ride` so the child starts from the same runtime.
- `RIDE_COMMAND` — the user-visible invocation this session launched under for telemetry and the banner:
  the reconstructed `ride solo|along …` command with its flags, `ride resume <ref>` for a resume, or the `summon --join …` request that started a joined member.
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
  Resolved once per launch, set in both isolations, and set by the summon lowering for a child
  — whose repository is its summoner's, so the two credit the same human.
  Absent for a detached launch, which has no commits to credit, and for an attachment declaring no identity
  — which the launch warns about.
  The names are owned by `bro.workspace.human`, whose `session_human()` reads them back:
  a session commits as its bro, so the human it credits reaches its commits only through what the launch carried (`bro.workflow.co_author`).
- `BRO_HOLD` — the session's user-involvement level (`unattended | detached | attended | guided`);
  the neutral `do-ride` layer exports it from `--hold` for every harness, overwriting any ambient value (a session launched from inside another must not inherit its hold), before anything the session spawns inherits the environment.
  Read by the claude service-server assemblies in `bro/bro.py` to gate the `raise` service tool's mount on the unattended level.
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
  Set by every managed launch for both harnesses
  — `ride`’s own in both isolations, and a summon’s child spawn.
  `do-ride` requires it before starting the harness.
  Read by `bro/monitor` — a process without it is in no managed session and so has no trail pointer to publish and no recording health to report.
- `RIDE_SUMMONED` — marks a run as a summoned child.
  The env name is owned by `bro.summon` and read back through its `summoned()` predicate;
  set by the summon lowering and by the `--summoned` launch,
  read by the Claude runner to emit the child's run lifecycle over the broker channel (a bro-run child emits from `bro.native.runner.Runner.run`,
  a summoned interactive one its `trail` mark from `Runner.send`'s first turn), by the service-server build to mount the `answer` tool,
  and by `ride banner` and `bro.prompts.session_fragment` so the run can tell in-session that it owes a summoner an answer.
- `RIDE_PARTY_MEMBER` — the channel-derived member name when a session joined an existing party.
  Its presence makes the banner and session prompt state that the tree is shared, and makes `do-ride` skip persona workspace provisioning.
- `RIDE_TRAILS_ROOT` — names the local trails backend's root (`bro.workspace.paths.trails_dir`) to a session that cannot derive it.
  An unboxed session gets its ride's own root, since its closed environment carries no data home and an inherited value would be its parent's;
  a boxed party member gets its own `party/<member>/trails` under the party mount, since the ride-wide `/var/ride/trails` bind belongs to the first session and its scope may not have one,
  and the member's supervisor adopts those trails into the ride's own store when it settles.
- `RIDE_MAY_SUMMON` — the run's own effective summon allow-list, comma-separated and empty when it may summon nothing.
  The env name and its encoding are owned by `bro.summon`;
  set by the launch surfaces for a session root and by the summon lowering (or, for a manual child, the `--summoned` launch from the pending record) for a summoned child (its own resolved list, never its summoner's),
  read by `ride banner` to render the fact, and by `bro.prompts.session_fragment` to tell the surface how to arm or poll the quest watch;
  the tool fold admits that command through bro-native's roster-gated `job` for such a run, and `watch-run quest watch` with `watch-next` through Claude's `Bash`.
  Read-only in the session:
  the launcher authorizes against its own copy, so only a relaunch (or the summon that spawns a child) changes what it may summon.
- `RIDE_PERMITS` — the run's own effective party permits under the same encoding, publication, and host-side enforcement rule.
  `ride banner` renders each with its `:` grammar marker.
- `BROKER_MISSION` — the broker mission this run undertakes.
  Every spawner writes it beside the channel;
  a manual summon's token is its mission id and carries the same value into the user-launched session.
  The bro workflow exposes this mission as the run's own quest, named `self` on its quest surfaces.
- `BROKER_TALK` — the sorted, comma-separated rights of the run's own quest;
  empty means mute and unset means the launcher published no fact.
  Every spawner writes it beside `BROKER_MISSION`, and a manual summon's pending record carries it into the user-launched session.
  `bro.summon.talk()` feeds the banner, the session prompt's `#talk` fact, and the Claude tool fold;
  the host journal remains the enforcing copy.
- `RIDE_PUBLISHED_PORTS` — a worker container's comma-separated `<container>=<host>` loopback port mappings, including an empty value when it declares none.
  Session containers do not receive it.
- `RIDE_IN_CONTAINER=1` — the runtime image's own, marking a process running in an image this runtime built.
  `bro/workspace/paths.py:trails_dir` uses it only to resolve the image's fixed trails mount.
  Session placement comes from `RIDE_ISOLATION` instead.
- `BRO_STORE` — the exclusive scoped credential-store directory delivered by the launcher.
- `BRO_INSTALL_KINDS` — the space-separated declared kinds hydrated into that store, including an empty value when none resolved.
  `do-ride` requires these two variables together and installs only those kinds' hooks.
- `BRO_INSTALL_DIR` — the session's directory for credential-hook output.
  Each launcher sets its own value, overriding ambient session state;
  a direct `do-ride` invocation derives the current isolation's session environment directory and exports it.
- `RIDE_RESOLVED_LLM` — the JSON encoding of the exact LLM recipe the launcher validated, scoped, and recorded.
  `do-ride` uses it instead of resolving an omitted or partial `--llm` against defaults that may have changed before a resume.
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

The Claude runner under `do-ride` starts a `ride.claude.trail-recorder` daemon (via `ride/ride/claude/recorder.py`) before launching Claude and stops it after Claude exits
— the stop is the recorder's final append and trail end.
The daemon records continuously through the backend the session's own `trails` credential selects, and to the local filesystem where its unpicked empty instance is absent.
A selected or present `trails` name must load like every other held credential;
`--no-trails` removes the recording need and starts no recorder daemon.
For local storage, the launch description binds the host's `<runtime-root>/trails` at `/var/ride/trails` inside the container, so the trail survives container removal and is visible to host-side `rewind` while the session runs.

The recorder records one claude-harness *trail* per transcript segment and appends newly completed lines at its polling interval (`ride/ride/claude/trail_recorder.py` owns transcript acquisition;
the core trails schema and Claude lineage verdict remain in `bro.trails`).
A verified same-segment resume reopens that segment's trail where the previous lifetime stopped, so a process restart adds no edge;
an interactive resume's history copy opens a fork trail pointing at the prior one, and a whole conversation is the fork chain `rewind show` walks.
The daemon nominates nothing:
it blazes with the transcript's lineage evidence and the store's harness resolver decides the edge, or declines a transcript claude has not finished writing (`bro/trails/AGENTS.md`).
The trail carries the launch recipe, plus the managed-session facts a bro-harness session's trail carries alike
— the session's location, its launch line, and the attached tree's state in the header's `git`, read off the session env by `bro.trails.record.session` in either harness's recorder —
and the recorder publishes its current trail id to the session's trail pointer,
from which summon control stamps the session's summoned children with `summoned_by.trail_id` (see "Summoning another bro").
The daemon's stderr goes to `claude/session-recorder.log` in the session state dir;
its durable signal is the health file it beats on every attempt (`bro/monitor/health.py`), which the statusLine and `ride banner` surface.
The beat carries the outcome and the age at which the reader must give the daemon up, so both a recorder that fails loudly and one that is simply gone
— killed by a signal, OOM, a crashed container process
— are reported while the session runs.
A daemon that cannot be started at all never reaches that signal:
the runner ends the launch, since a session that believes it is recording and is not is worse than one that refuses to start.
A session whose transcript ends in a `raise` service-tool call gets its trail ended as `raised` with the reason as `end.detail` (a later real user message — a resume moving past the abort — clears it),
keeping an unattended session's abort queryable without parsing the jsonl.

Clone creation and provisioning are owned by `ride` directly (unboxed isolation — see "Unboxed isolation" above);
no Claude Code hooks are wired for either.
