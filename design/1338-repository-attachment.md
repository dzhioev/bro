# Design: workspace attachments, provisioning and the runtime toolset

This file is the specification of [#1338](https://github.com/dzhioev/bro/issues/1338).
It lives on the branch `design/1338-repository-attachment`, which is never merged:
the pull request that carries it is a design review only.

## 1. Goal, scope and constraints

Goal: a ride workspace's tree is the bro's own directory, and what the host provides for a session to work with arrives in it as attachments.
An attachment is a typed unit of data in its own subdirectory of the tree;
a git repository is one type of it, the only one so far.
An attached repository describes itself in a language-neutral `.bro/config.toml` and is provisioned by the session itself, under the session's own identity.
Every session, boxed or unboxed, runs on the installed bro toolset alone, frozen once per root and installed for the host and for containers the same way:
the launcher fetches every git dependency, so an install inside a container needs no credential.

What it enables, for example:

- A joined reviewer writes an oversized round report beside the shared clone and mints it from there, never writing to the clone.
- A repository whose `setup.sh` is a host installer is attached without it running, and a repository that wants provisioning names its command, whatever its file is called.
- A project that pins a private git dependency launches boxed:
  the session's own `github` identity installs it into the project's environment, and a private pin of the toolset reaches the runtime volume through the launcher.
- A non-Python repository declares its provisioning command.
- Upgrading one of the project's development tools, its linter say, no longer creates a new runtime bundle.
- A distribution adds a type of attachment without changing ride's workspace machinery.

The owner settled the design's decisions on 2026-11-18;
§16 records each one and the section that applies it.

Out of scope, each with where it belongs:

- launch configuration, meaning the default bro, the harnesses, the model recipe, the credentials, the authority and the summon depth,
  which stays the installation's packaged `bro-settings.toml` and the host configuration `~/.bro.json` (`bro/reference/ride.md`, "Home settings"):
  nothing a repository's `.bro/config.toml` says changes it;
- several attachments in one workspace, and the per-attachment facts they need, which are [#1341](https://github.com/dzhioev/bro/issues/1341)'s;
- a provisioning hook for workspace creation alone, since one command at every start covers the framework's consumers;
- a package cache shared between boxed workspaces, which §8.3 rules out;
- private package indexes, authenticated archive pins and the submodules of a git pin as sources of the runtime volume (§10.3);
- the benchmark's relocatable bundle, which its host builds from the workspace (`benchmark/AGENTS.md`);
- migrating attached workspaces recorded in today's layout, which are refused instead (§4.2).

Constraints:

- framework code stays consumer-neutral and core imports no other workspace member, so the repository configuration's reader, which `bro-oops` shares, lives in core (§6.2);
- backward compatibility is not a constraint (`AGENTS.md`, "Conventions");
- a violated assumption fails fast (the development style policy);
- every boxed bind source resolves under the runtime root, which the daemon preflight verifies (`bro/reference/ride.md`, "Boxed launches and Docker daemons");
- no credential reaches a runtime bundle, a runtime volume, an image layer or the materializer;
- a runtime bundle reproduces itself:
  a freeze taken from its own host venv lands on it (`bro/reference/ride.md`, "Runtime bundles");
- a session's stdout carries only its data output, which `ride solo` writes as the reply.

## 2. Concepts

Terms the repository already defines (`bro/reference/ride.md`), as this design uses them:

- **Workspace** — `<runtime-root>/workspaces/<name>/`, the globally named unit ride creates, runs sessions in and cleans up, holding a tree and the records the launch machinery keeps about it.
  "Workspace" names only this unit.
- **Workspace record** — the workspace's `workspace.json`, written once at creation.
- **Isolation** — `boxed`, in a container, or `unboxed`, on the launcher's filesystem, fixed at creation.
- **Launcher** — the `ride` process that prepares a workspace and supervises its session;
  its **ambient git authentication** is whatever the launching user's own git configuration supplies.
- **`do-ride`** — the session executable every launcher runs inside a prepared workspace.
- **Joined member** — a session that joined another session's party and shares its workspace.
- **Installation** — the Python environment `ride` is invoked from, which pins one revision of the framework.
  A host holds one for each repository that adopted the framework, and they share one runtime root.
- **Runtime bundle** — the frozen, content-addressed snapshot of the installation that invoked `ride`:
  a manifest of **pins**, `name==version` for an index installation and `name @ git+<url>@<commit>` for a git one, and the wheels of its **local sources**, named by its hash.
  Its **bundle root** is the directory it persists in, `<runtime-root>/runtime/<hash>/`.
- **Host materialization** — the bundle installed into `host/venv` on the launcher, which the launcher re-executes from.
- **Runtime volume** — the Docker volume `ride-runtime-<hash>`, mounted read-only at `/var/ride/runtime` in every boxed session of that bundle;
  the **materializer** is the throwaway runtime-image container that installs the bundle into it, the **container materialization**.
  An unboxed session runs the host materialization, a boxed one the runtime volume, both of its root's one bundle.

Terms this design introduces or redefines:

- **Tree** — the workspace's writable directory, `tree/` on the launcher and `/tree` in a container (§3).
  It is no longer a git clone.
- **Attachment** — a unit of data the host provides for a session to work with, of one **attachment type**, under an **attachment name** that is its directory directly inside the tree (§5).
  A workspace with no attachment is **detached**.
- **`repo` attachment** — the attachment type that is a git repository:
  its **clone** is an independent git clone in the attachment's directory (§5.2).
  "Clone" names only that checkout.
- **Start directory** — the directory a session starts in (§3).
- **Repository configuration** — the file `.bro/config.toml` at a repository's root (§6).
- **Provisioning command** — the shell command a repository configuration declares to make its clone ready for work (§6.1).
- **Provisioning** — what `do-ride` does to the clone before the harness starts:
  the provisioning command, then the persona's provisioning steps (§7).
- **Toolset** — the installed distributions that plug into bro, the **toolset roots**, together with everything they depend on (§9).
- **Active extra** — an optional-dependency group of an installed distribution whose requirements the installation satisfies (§9.1).
- **Staged pin** — a bare git repository holding a git pin's commit with its history, which the launcher fetches before it materializes a bundle, for the host or for a container (§10).
- **Wire path** — a path a session names to its host in a request, absolute as the session sees it (§11.1).

## 3. Workspace layout

```
<runtime-root>/workspaces/<name>/
  workspace.json       the workspace record (§4)
  tree/                the tree, /tree in a container
    <attachment name>/ an attachment, such as a repo attachment's clone
  lock, exit, resume.json, session.log, session/, <harness>/, party/
                       the records `bro/reference/ride.md`, "Workspaces", describes
```

The tree belongs to the session:
ride creates it empty, places each attachment in it before the session's first start, and writes nothing else into it.
What a session writes beside its attachments stays in the tree, inside the session's artifact reach (§11.2) and outside every git repository.

A session with one attachment starts in that attachment's directory, and a detached session starts in the tree.
That holds in both isolations:

| Workspace | Boxed start directory | Unboxed start directory |
|---|---|---|
| a `repo` attachment named `bro` | `/tree/bro` | `<runtime-root>/workspaces/<name>/tree/bro` |
| detached | `/tree` | `<runtime-root>/workspaces/<name>/tree` |
| detached, `--tree PATH` | — | `PATH` |

A joined member starts in its party's start directory, and `ride exec` enters a running boxed workspace there too.
An external tree (`--tree`) still belongs only to a detached unboxed workspace.
Where a session with several attachments starts is #1341's to settle.

For example, `ride along --repo ~/bro -w fix-12 bro-dev` creates the clone at `<runtime-root>/workspaces/fix-12/tree/bro/` and starts the session in `/tree/bro`;
a reviewer joining that party writes its notes to `/tree/review/` and never touches `/tree/bro`.
`ride along -w notes bro` starts in an empty `/tree`.

## 4. The workspace record

### 4.1 Shape

A workspace's record with a `repo` attachment:

```json
{"isolation": "boxed", "attachments": [{"type": "repo", "name": "bro", "repo": "/home/me/bro", "branch": "workspace-fix-12"}], "throwaway": false, "tree": null}
```

A detached one:

```json
{"isolation": "boxed", "throwaway": false, "tree": null}
```

- **`attachments`** — present exactly when the workspace has an attachment, as a non-empty list of objects, each holding:
  - `type` — the attachment type, a name registered in `bro.attachment_types` (§5.1);
  - `name` — the attachment name, unique within the workspace;
  - the fields its type records, which for `repo` are `repo`, the resolved checkout path or normalized git URL, and `branch`, the clone's branch, `workspace-<name>` for a new workspace.
- `isolation`, `throwaway` and `tree` keep their meanings (`bro/reference/ride.md`, "Workspaces").

A launch naming an existing workspace with attachments that differ from the recorded ones in type or in their type's identity, `repo` for a `repo` attachment, is refused, as reusing a name with a different `repo` is today.

### 4.2 Records in today's layout

A record carrying top-level `repo` and `branch` is not in the current shape, so a command that names its workspace refuses it by name and points to `ride clean --force <name>` (`bro/reference/ride.md`, "Runtime state").
Its directory is left as it is, so its files stay readable for recovery.
A kept attached workspace therefore lands or pushes its work while an installation from before this design can still open it (§13.3).

A command that enumerates workspaces passes over a record outside the current shape instead of failing on it,
since installations of different framework revisions share one runtime root and meet each other's records there (§13.2):

- `ride list` lists the workspace by name, marked as unrecognised, with the `ride clean --force <name>` hint in place of its attachment and subject;
- a `ride clean` that names no workspace leaves it and reports it by name,
  and removes no managed mirror while such a record exists, since it cannot read which mirror the record names;
- the check that an external tree is recorded by one workspace (`--tree`) reads the records it recognises.

Today's detached record is already in the current shape, and the session record a resume reads keeps its fields, `repo` among them (§5.1),
so a launcher of either layout opens the other's detached workspace.
An unboxed one keeps its tree's path and resumes as before.
A boxed one keeps its files, but its sessions see the tree at `/tree` where they saw `/workspace`,
so a harness that keys its session state to the start directory (§5.5) finds none recorded there:
`ride resume` of a Claude session refuses, naming the directory it looked in, and a new launch on the workspace starts a fresh session over the files it holds.
A session of the bro harness resumes from its trail, whose earlier turns name paths under `/workspace`.

## 5. Attachments

### 5.1 Attachment types

An attachment type is a registered object of `bro-ride`'s attachment contract, `<type> = "<module>:<object>"` in the `bro.attachment_types` entry-point group, loaded lazily by name as harnesses and worker types are.
The group's `bro.` prefix makes a type's distribution a toolset root (§9.1), so `do-ride` finds the type in the bundle.
A type supplies:

- **resolution** — turning its launch value into the attachment's name and recorded fields;
- **preparation** — filling `tree/<attachment name>/` on the launcher before the session's first start;
- **session facts** — the environment variables it contributes to the session;
- **a session-start step** — what `do-ride` runs for it after the credential hooks (§7.2), which may be nothing;
- **inheritance** — the attachment a started or manual child gets from its summoner's;
- **lifecycle** — what it adds to the workspace's cleanliness, what `ride clean` removes for it, and what `ride list` shows of it.

The neutral launcher, the workspace model, the container machinery and the bro worker's lowering reach a workspace's attachments only through this contract, and run no git operation of their own.
`do-ride` learns its session's attachments from the launcher, which passes each one's type, name and recorded fields with the session's launch:
the workspace record lies outside what a boxed session mounts.

A launch carries at most one attachment until #1341 lifts the limit.
`--repo PATH|URL` is the only launch syntax, adding a `repo` attachment;
a type another distribution registers has no launch syntax until a generic one comes with a second type (§15).
A started or manual child gets its root's attachment through the type's inheritance, and a detached root starts detached children.

### 5.2 The `repo` type

The `repo` type ships in `bro-ride`.
It owns what an attached repository is today:
resolving `--repo` to an identity and the managed URL mirrors (`bro/reference/ride.md`, "Managed URL mirrors"), the bases a clone is created at, the clone and its session facts,
provisioning as its session-start step (§7), and the human's git identity read from the attachment's own git configuration (`ride/ride/identity.py`).

Its attachment name is the repository's name, derived once at creation and recorded:

- A checkout path's name is its last component:
  `/home/me/bro` gives `bro`.
- A URL's name is the last segment of its normalized path, with a trailing `.git` removed:
  `https://github.com/dzhioev/bro` gives `bro`, and `git@github.com:acme/widget.git` gives `widget`.

A derived name that is empty, `.` or `..` refuses the launch, naming the attachment.
A child derives its name from the same repository, so it is its root's.

### 5.3 Preparing the clone

On the workspace's first launch, before its container is created when boxed or its session process starts when unboxed, the launcher creates the clone at `tree/<attachment name>/` with today's clone machinery and its ambient git authentication:
an independent clone of the checkout or managed mirror, with `origin` retargeted upstream and the attachment's `refs/remotes/origin/*`,
the recorded branch at the resolved base, the initialized submodules, and no alternates (`bro/reference/ride.md`, "Boxed isolation").
It prepares the clone in a temporary directory of the workspace directory, beside the tree, and publishes it atomically,
so a killed launch leaves nothing in the tree.
The clone exists when `tree/<attachment name>/.git` is a directory;
a `tree/<attachment name>` that exists without one refuses the launch.
Later launches leave the clone exactly as the session left it.

### 5.4 Bases

The base is the commit a new clone's branch starts at;
a launch uses it only to create the clone.
The rules are today's:

- a root bases on `--into` when given, otherwise on a URL attachment's freshly fetched `origin/HEAD` or a checkout's `HEAD`;
- a started child bases on its request's `into` when given, otherwise on the `HEAD` of its summoner's clone, read at summon time
  — the clone in the workspace the summoner runs in, its party's for a joined summoner —
  after transferring into the attachment the commits it lacks, as `bro.workspace.git.resolve_head` does;
- a manual child bases on its request's `into` when given, otherwise on the `HEAD` of its summoner's clone as it stands when the user launches the child:
  its pending record names that clone rather than a commit, so a commit the summoner makes after the summon reaches the child;
- a detached root refuses `into`.

### 5.5 Session facts

The `repo` type sets `RIDE_REPO`, `RIDE_REPO_URL`, `RIDE_BRANCH` and `RIDE_BASE_SHA`, and the human's `BRO_HUMAN_NAME` and `BRO_HUMAN_EMAIL`, with today's meanings,
reading `RIDE_REPO_URL` and `RIDE_BASE_SHA` from the clone where today they are read from the tree.
They stay singular until #1341 replaces them.

Every launcher sets `RIDE_HOST_TREE`, which replaces `RIDE_HOST_WORKSPACE`, to the tree's launcher-side path;
the banner reports the tree as `tree_host_path` and `tree_container_path`, which replace `workspace_host_path` and `workspace_container_path`.

A harness keys the state it keeps per working directory to the start directory:
the Claude harness's project directory and trusted path name `/tree/bro` in the example of §3, and `/tree` only for a detached boxed session.
Every container's entrypoint marks every directory safe for git (`safe.directory=*`), so it reads no attachment fact.

### 5.6 Lifecycle

- A workspace with a `repo` attachment is clean only when its last session exited `0`, and a detached managed tree only while it is empty, as today.
- `ride list` shows a `repo` attachment's `repo`.
- `ride clean` keeps a managed mirror while some record's `repo` attachment names its URL, and removes a workspace whose attached checkout or mirror is gone only with `--force`, as today.
- The launch configuration a root reads depends on no attachment:
  the reading stops carrying the attachment identities and repository sections it holds today, which no rule reads (`ride/ride/authority_reading.py`), and `ride scope` drops `--repo`.

## 6. Repository configuration

### 6.1 The file

A repository describes itself to bro in `.bro/config.toml` at its root:

```toml
# runs at every session start, so it must be idempotent
provision-command = "./setup.sh"

[devoops]
target-registry = "oops.deploy_targets:registry"
```

- **`provision-command`** — optional, a non-empty string, run as §7 describes.
- **Owner table** — any table, a section another component owns, carried verbatim and interpreted only by that owner:
  `[devoops]` is `bro-oops`'s target registry.
- Any other top-level key refuses the file wherever it is read, naming the file and the key.

A repository without `.bro/config.toml` has no repository configuration, and its sessions set themselves up from its own instructions, such as its `AGENTS.md`.
`[tool.bro]` in `pyproject.toml` is no longer read by anything:
its owner tables move to `.bro/config.toml`, and its `build-context-command` goes with the project image it served (§8.1).

### 6.2 Readers

One reader in `bro.workspace`, replacing `bro/workspace/project.py`, parses and validates the file for every consumer.

- The `repo` type's session-start step reads `<clone>/.bro/config.toml` at each start, as the clone holds it then, so a commit that changes the file takes effect at the next start.
- `bro-oops` reads `[devoops]` from the file at the root of the repository it runs in, found as today (`bro.workspace.paths.find_project_root`).
- The launcher reads nothing from a repository's content.

## 7. Provisioning

### 7.1 When

Provisioning runs at every start of a session with a `repo` attachment that is not a joined member:
a root's launch and each of its resumes, a started child, and a manual child, in both isolations.
A detached session and a joined member run none;
a joined member's party has already provisioned the shared clone.

### 7.2 Order

`do-ride` starts a session in this order:

1. the session environment it sets today:
   the bro's git identity, the `RIDE_*` facts, `BRO_HOLD` and `RIDE_RUNNER_PID`;
2. the scoped credential hooks;
3. each attachment's session-start step, which for a `repo` attachment is its provisioning command when the repository configuration declares one;
4. when the session has a `repo` attachment, the persona's provisioning steps (`BaseBro.provision_workspace`), given the clone's root;
5. the harness's session preparation, then its run.

A joined member runs steps 1, 2 and 5.
The credential hooks come first so provisioning fetches what only the session's identity reaches (§7.4).
The command comes before the persona's steps so a step that leaves an existing file alone, such as the dev family's footer hooks, keeps what the repository installs.

### 7.3 Running the command

- The command runs as `sh -c <provision-command>` in the clone's root, in `do-ride`'s environment at that point:
  the session's closed environment with the credential hooks' environment over it, so git and `gh` act as the session's `github` identity, and the session's `PATH` with the hooks' `bin` first.
- Its stdin is empty, so a command that prompts reads end of file instead of waiting.
- Its stdout and stderr go to `do-ride`'s stderr, which keeps them out of a one-shot session's reply.
- An exit status of `0` continues the start.
  Any other status ends it:
  `do-ride` logs `provisioning command failed with exit status <n>: <command>` and exits with status 1 before the harness starts,
  so a started child ends failed with its output tail, as any child whose session exits nonzero does.
  A repository configuration the reader refuses ends the start the same way, with the reader's message.
- The command has no time limit of its own;
  a child's summon timeout bounds it as part of the child's run.

The command runs at every start, so its idempotency is the repository's contract, stated where the key is documented.

### 7.4 What it covers

The project's own environment is provisioning's:
a uv project's `uv sync` in its provisioning command, or its first `uv run` where it declares none, installs the lock's git sources through the session's own identity.
A session scoped without access to a private dependency therefore cannot install it:
its provisioning, or its first `uv run`, fails naming the dependency.

A declared command makes the setup the same in every session, so an unattended session never mistakes a missing environment for failing code;
a repository that declares none still works, its sessions setting themselves up as its instructions say.
This repository declares `provision-command = "./setup.sh"`, whose sync no longer compares a baked environment's manifests.
A plain uv project needs no declaration.

### 7.5 What it replaces

Nothing runs a file by its name any more:
the entrypoint's `setup.sh` call and project-venv link, `RIDE_VENV_MANIFEST`, and the unboxed launcher's own `setup.sh` run (`ride/ride/workspace/worktrees.py`) go.
The entrypoint keeps aligning the `ride` user with the tree's owner, re-owning the scoped store, marking directories safe for git, and executing `do-ride`.

## 8. Images and package caches

### 8.1 One image

Every boxed session runs the runtime image, whatever its attachments.
The project image goes, and with it `project.Dockerfile`, the project build context and its manifests, `build-context-command`, the `ride-attachment` label and the project-image pruning;
removing a workspace's root-owned files uses a present runtime image.
The runtime image's tag stops hashing `project.Dockerfile`, so the landing builds one new runtime image.

### 8.2 The project's environment

What provisioning builds in the clone, such as its `.venv`, lives on the tree's mount, so it persists through the workspace's later sessions and resumes;
a new workspace builds it at its first start.
A boxed session's uv cache stays in its container's own layer, and the runtime image sets `UV_LINK_MODE=copy`, since that cache and the clone lie on different filesystems.

A cold full sync of this repository, `uv sync --frozen --all-packages --all-groups --all-extras`, took 6.8 s in a boxed session, and 5.4 s with a warm cache (§17).
The environment it builds is 294 MB, which each boxed workspace holds as its own copy for as long as it is kept,
where the project image held one copy for every workspace of a lock.

### 8.3 No shared cache

Boxed workspaces share no package cache.
A shared one would hand a private dependency one session fetched to a session scoped without access to it, which would then install it from the cache,
and it would let any box plant entries that every other box trusts.

## 9. The runtime toolset

### 9.1 What a bundle carries

A freeze carries the invoking installation's toolset instead of every installed distribution.

- The **toolset roots** are the installed distributions that declare at least one entry point in a group whose name starts with `bro.`, the framework's entry-point namespace (`bro/reference/extending.md`, "Entry-point groups").
- The **toolset** is the smallest set that holds the roots and, for each member, the distribution each of its counted requirements names.
  A requirement of a member counts when its environment marker holds for the installation, either unconditionally or under one of the member's active extras.
- An extra of an installed distribution is **active** when the installation satisfies every requirement the extra adds:
  each names a distribution installed at a version the requirement allows, with each extra the requirement itself names active.
  Extras that require each other in a cycle are active together unless a requirement outside the cycle is unsatisfied.
- A counted requirement the installation does not satisfy fails the freeze, naming the requirement and the member.

Each member is pinned and carried as today, by its recorded provenance;
a distribution outside the toolset leaves the manifest, the wheels and the hash.

For example, this checkout's development environment holds 116 distributions, 18 of them toolset roots:
`bro` and every member of the workspace.
The extras its sync installs, `bro[aws]`, `bro[runtime]` and `bro-openai[client]` among them, are active, so the OpenAI SDK and `boto3` stay in the toolset.
The 18 distributions left out are development tools and what only they need (§17):
the checkers `deptry`, `pyright`, `ruff`, `shellcheck-py`, `types-python-dateutil` and `types-pyyaml`;
the test tools `moto`, `pytest-aiohttp`, `pytest-asyncio` and `pytest-xdist`;
and what only those require, `execnet`, `markupsafe`, `nodeenv`, `requirements-parser`, `responses`, `tomli`, `werkzeug` and `xmltodict`.

A bundle still reproduces itself.
Its host venv holds exactly the toolset, whose roots are the same there;
an extra active in the frozen installation has its requirements in the toolset, so it is active in the host venv, and an extra active in the host venv is satisfied by distributions the frozen installation also holds.

### 9.2 Consequences

- A member that imports a distribution it does not declare fails that import inside a session while the launcher may import it;
  the remedy is the member declaring its dependency.
- A distribution outside the toolset that cannot be pinned reproducibly, such as a vanished local source, no longer fails a launch.
- A project that registers its bros from its own distribution makes that distribution a root, so its whole dependency closure joins the toolset;
  a sidecar or a dedicated persona member keeps the application's dependencies out, which BOOTSTRAP's packaging choice states.
- The bundle hash changes only when the toolset does.

The manifest's format, host materialization, the re-execution, `--runtime-bundle`, the bundle locks and their cleanup stay as they are.

## 10. Git pins

### 10.1 Staging

Before it materializes a bundle, for the host or for a container, the launcher stages the bundle's git pins, holding the bundle's materialization lock as the materialization does.
For each distinct URL among the bundle's git pins, it creates a bare repository under `<bundle root>/git-pins/`
and fetches into it each commit the bundle pins from that URL, with `git fetch <url> +<commit>:refs/commit/<commit>`.
That is the fetch the installer itself makes for such a pin:
it brings the commit with its whole history and the tags that point into it.
A build that derives a distribution's version from those tags therefore yields from the staged pin the version the invoking installation holds,
which a copy of the one commit would not (§17).
The fetch runs under the launcher's ambient git authentication with prompts disabled, as mirror fetches do.

The URL is the pin's, without its `git+` prefix, its `@<commit>` and its fragment, which is how uv hands it to git.
Every git pin is staged, public or not, since the launcher cannot tell which ones a container could fetch.
A failed fetch fails the materialization, naming the pin and git's message.
A `git-pins/` directory a killed launch left behind is replaced.
A bundle without git pins stages nothing.

### 10.2 Installing through the staged pins

The install runs with one redirect per staged URL added to its git configuration through `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_<n>` and `GIT_CONFIG_VALUE_<n>`:
`url.<staged repository>.insteadOf = <url>`, where the staged repository is a `file://` URL of its path as the install sees it.
The entries follow those the launcher's environment already carries, such as the ones a session's `github` credential hook sets for a launcher started inside that session,
so the variables add to whatever configuration git reads otherwise rather than replacing it.

- The host materialization's install reaches `<bundle root>/git-pins/` at its own path, over the launching user's git configuration.
- The materializer reaches it at `/bundle/git-pins/`, through the read-only bind of the bundle root it already has, and its install has no other git configuration.
  It runs as root over repositories the launching user owns, which git refuses to serve until `safe.directory` names them,
  and git passes no environment-carried configuration to the process that serves a local repository (§17).
  So before the install the materializer names each staged repository in the `safe.directory` of its own system configuration, which goes with the container.

The pins themselves are unchanged, and the installed record keeps each pin's original URL and commit (§17).
The launcher removes `<bundle root>/git-pins/` once the materialization ends, whether it succeeded or failed, so nothing staged persists:
the bundle, its hash and both materializations are what they would be without staging.
Every materialization goes this way:
the host's, and the container's for session containers, worker containers and foreground worker runs.

### 10.3 Limits

Inside the materializer, staging covers each git pin's own commit and history, and nothing it needs beyond:

- a staged pin's submodules are fetched from their own URLs, without credentials;
- a distribution from a private package index, and a remote-archive pin behind authentication, are downloaded without credentials.

Each still fails the container materialization with the installer's error, which names the distribution.
The host materialization reaches them through the launching user's own configuration, as today.

Staging fetches a pin by its commit id, which a server speaking only git's original protocol refuses unless it is configured to allow it (§17).
Such a pin fails the materialization at staging, for the host and for a container alike, naming the pin and git's message (§10.1).

## 11. Paths a session names to its host

### 11.1 Wire paths

Every path a session names to its host in a request, an artifact to mint or a benchmark job's files, is a wire path:
absolute, as the session's own processes see it.
The client that builds the request makes a relative path absolute against its working directory, lexically and without resolving symbolic links, and checks nothing else.

The host owns the check.
Its description of each peer carries, beside the peer's tree, the path the peer sees that tree at:
`/tree` for a peer that runs in a container, which is a boxed session, a joined member of a boxed party or a worker container, and the tree's own path for an unboxed peer.
One core helper, replacing `bro.worker_types.tree_path`, maps a wire path from that view to the tree and resolves it on the host:

- for a peer in a container it replaces the leading `/tree` with the tree's launcher-side path, refusing a path that does not lie under `/tree`;
- for an unboxed peer, which names paths of the launcher's own filesystem, it takes the path as it stands,
  so a path spelled through a symbolic link to the runtime root stays valid.

It then resolves the result and refuses it when it lies outside the resolved tree.
A relative path is refused before any mapping, and every refusal names the path.

### 11.2 Minting artifacts

`artifact mint <path>`, and the client function under it, accept a path relative to the caller's working directory or an absolute one, and send it as a wire path.

From `/tree/bro` in the workspace `fix-12` of §3:

- `artifact mint report.md` sends `/tree/bro/report.md`, which the host reads at `<runtime-root>/workspaces/fix-12/tree/bro/report.md`;
- `artifact mint ../review/r3.md` sends `/tree/review/r3.md`;
- `artifact mint /etc/hosts` reaches the host and is refused;
- a symbolic link inside the tree that points to `/opt/cache` resolves outside the tree on the host and is refused.

The webview mints paths relative to its working directory, its tree, so they keep their meaning, and its artifact view moves from `/workspace/artifacts` to `/tree/artifacts`.

### 11.3 A joined reviewer's oversized round

The eyebro's `review` spell writes an oversized round to a unique directory in the tree outside the clone, at the tree path its banner reports, mints it from there, and removes that directory;
the stopgap that stages it under the clone's `.git/review-rounds/` goes.

### 11.4 The benchmark job

`benchmark-run` sends its job's `config` and, in a new `project` field, the benchmark project's directory, both as wire paths;
the job maps both through the helper of §11.1 and refuses a `project` without a `pyproject.toml`.
Its environment stays beside the tree, at `<workspace>/benchmark-venv`.

## 12. Documentation, this repository and its consumers

- `bro/reference/ride.md`:
  the mode verbs' attachment, "Runtime bundles" (§9, §10), "Workspaces" (§3, §4),
  "Attached-tree configuration" rewritten as attachments and the repository configuration (§5, §6),
  "Unboxed isolation" and "Boxed isolation" (§5.3, §7.5, §8.1), "Sharing artifacts between peers" (§11.2), and "Forwarded env vars" (`RIDE_HOST_TREE`, the start directory);
  provisioning's contract, its idempotency included, is stated there once.
- `BOOTSTRAP.md`:
  the root-lock image gates and project-image proofs go;
  a repository with a setup flow declares it as `provision-command` in `.bro/config.toml`, replacing the `setup.sh` rule;
  `[tool.bro]` becomes `.bro/config.toml`;
  and the packaging choice states what reaches the toolset (§9.2).
- `bro/reference/extending.md`:
  the `bro.attachment_types` group, and the `bro.` entry-point namespace defining the toolset roots.
- `bro/artifact.py`'s wire contract:
  `artifact.mint` carries a wire path.
- Every `/workspace` path a session or its tooling names becomes `/tree`:
  the container setup, the Claude harness, the banner, the trail display, the webview, and the prompts and spells that cite it.
- The maps (`ride/AGENTS.md`, `ride/ride/workspace/AGENTS.md`, `ride/ride/setup/AGENTS.md`, `bro/workspace/AGENTS.md`), `oops/README.md`, and the `[tool.bro]` mentions in `dev/bro/dev/spells/reflect.md` and `dev/bro/prompts/dev/bump_bro.md`.
- This repository:
  `.bro/config.toml` declares `provision-command = "./setup.sh"` and carries `[devoops]`, `pyproject.toml` drops `[tool.bro.devoops]`, and `setup.sh` drops `manifests_match`.
- Consumer repositories move their `[tool.bro]` tables to `.bro/config.toml` and declare their provisioning command in the commit that takes the framework revision.
- Regatta's builder pilot reads a build's result from the clone, now at `tree/<attachment name>/` as its workspace record names it;
  `RIDE_BRANCH`, `RIDE_BASE_SHA` and the clone's extra local branch stay as they are, and `RIDE_HOST_WORKSPACE` is now `RIDE_HOST_TREE`.

## 13. Rollout

### 13.1 What moves together

Everything one ride runs comes from one runtime bundle:
the launcher, the `do-ride` it starts, the session commands, a manual child's launch and the operator verbs (`bro/reference/ride.md`, "The launcher↔session contract").
So nothing inside a ride meets another revision of the framework, and a root keeps the bundle it froze for its whole lifetime:
a ride started before its installation moved finishes under today's layout, and its children with it.

The trail format does not change:
a trail's `location.dir` carries the tree's launcher-side path as it does today, so the trails server and the stored trails need no move.

### 13.2 Mixed revisions on one host

Installations move to this design one at a time, and they share a runtime root and a Docker daemon.
While their revisions are mixed:

- **Attached workspaces** — each launcher works with those of its own layout.
  A launcher of this design passes over the others in an enumeration and reports them by name (§4.2).
  A launcher from before it fails `ride list`, a `ride clean` that names no workspace and a `--tree` launch on the first attached record this design wrote, naming that workspace,
  until its installation moves or that workspace is removed.
  Its `ride clean <name>` removes the named workspace and then fails the same way, before it reaches the mirrors, the operator control files and the bundles,
  while its launches and its resumes are unaffected.
- **Detached workspaces** — either launcher opens them, with the consequence §4.2 states for a boxed one.
- **Managed mirrors** — shared, in an unchanged format;
  a `ride clean` of this design keeps them all while an unrecognised record exists (§4.2).
- **Runtime images** — the two revisions compute different tags, since the entrypoint and the image's inputs change (§7.5, §8.1), and each builds its own.
  A launcher prunes the superseded runtime tags no process holds, so launches alternating between the two revisions rebuild the image.
- **Runtime bundles and volumes** — content-addressed, so neither revision reads the other's.
- **Project images** — an installation from before this design still builds and prunes its own.
  No launcher of this design reads their `ride-attachment` label, so the ones left once every installation has moved are removed by hand,
  as `docker image rm` of the `bro/ride-project` repository's tags.

A repository and the launcher that attaches it move apart too:

- A launcher from before this design attaches a tree that already carries `.bro/config.toml` as it attaches any tree:
  it bakes the project image, runs `setup.sh` by its name and ignores the file.
- A launcher of this design attaches a tree from before its repository declared a provisioning command without provisioning it (§6.1).
  Such a session sets itself up as the repository's instructions say, until its branch takes the commit that declares the command.

### 13.3 Order

1. This design lands in the framework's repository.
2. Each installation moves, in any order:
   the framework's checkout by taking the landed commit,
   and a consumer's by a commit that takes that framework revision, moves its `[tool.bro]` tables to `.bro/config.toml` and declares its provisioning command (§12).
   A kept attached workspace recorded in today's layout lands or pushes its work before the last installation that can open it moves (§4.2).
3. Once every installation on the host has moved, the workspaces recorded in today's layout are removed by name with `ride clean --force <name>`,
   and the project images by hand.

An installation rolls back by returning to its previous revision;
the attached workspaces it created under this design are then the ones its launcher does not recognise.

The plan phase settles the stages and names the session that runs each step of the rollout.

## 14. Verification

Each route below needs automated coverage, the boxed ones in the host-only Docker stage:

- a boxed launch and an unboxed launch with a `repo` attachment start in the clone, with the tree writable beside it;
- a detached launch starts in the tree;
- a relative mint from the clone and an absolute one beside it are accepted, while a path outside the tree and a symbolic link out of it are refused by the host;
- a repository declaring a provisioning command runs it at the launch and at a resume, after the credential hooks, and a failing command ends the start before the harness runs;
- a repository with a `setup.sh` and no declaration starts without running it;
- an unknown top-level key in `.bro/config.toml` refuses the start;
- an attachment type registered by a test distribution prepares its directory, contributes its facts and runs its session-start step, reached through the contract alone;
- a freeze leaves out a distribution no root reaches, keeps an active extra's requirements, and its bundle reproduces itself;
- a bundle with a git pin whose URL neither the host nor the container can reach materializes for both through its staged pin,
  the container's install running as root over a staged copy the launching user owns, and leaves nothing staged;
- a git pin whose build takes its version from a tag on an earlier commit materializes at the version the invoking installation holds;
- a command naming a workspace whose attached record is in today's layout refuses it by name,
  while `ride list` and a `ride clean` naming no workspace pass over it and report it.

A private dependency fetched through a real `github` credential is verified live, once, by the verification phase.

## 15. Follow-ups

- [#1341](https://github.com/dzhioev/bro/issues/1341):
  several attachments in one workspace, replacing the singular facts of §5.5 with per-attachment ones.
- A generic launch syntax for attachment types, with the second type that needs it (§5.1).

## 16. Decision record

The owner settled decisions 1 to 15 on 2026-11-18, and decisions 16 to 19, which the review of this design raised, on 2026-11-19.

| # | Decision | Applied in |
|---|---|---|
| 1 | Attachment machinery stays in `bro-ride` rather than a separate distribution | §5.1 |
| 2 | Each attachment is a subdirectory of the tree, and a `repo` attachment is named after its repository and recorded | §3, §4, §5.2 |
| 3 | A language-neutral `.bro/config.toml` replaces `[tool.bro]`, carrying `provision-command` and owner tables | §6 |
| 4 | Provisioning stays declared and automatic: it runs in `do-ride` after the credential hooks, in both isolations, and fails the start | §7 |
| 5 | The project image and the `setup.sh` filename hook go | §7.5, §8.1 |
| 6 | Boxed workspaces share no package cache | §8.3 |
| 7 | Every materialization, host and container, reaches git pins through copies the launcher stages, the redirect layered over the git configuration through `GIT_CONFIG_COUNT` | §10 |
| 8 | A runtime bundle carries the bro toolset rather than the whole invoking installation | §9 |
| 9 | Workspaces recorded in today's layout are refused, not migrated | §4.2, §13 |
| 10 | Every session, boxed or unboxed, runs its root's one bundle | §1, §2 |
| 11 | A path a session names to its host travels absolute; the client makes it absolute and the host maps and checks it | §11.1 |
| 12 | An attachment is a typed unit of host-provided data, types registering through `bro.attachment_types`, with `repo` the one type | §2, §5.1, §5.2 |
| 13 | A launch carries at most one attachment and the singular facts stay until #1341; a session starts in its attachment's directory, or in the tree when detached | §3, §5.1, §5.5 |
| 14 | `--repo` stays the only launch syntax until a second type needs a generic one | §5.1, §15 |
| 15 | The workspace's writable directory is the tree, `/tree` in a container, and "workspace" names only the managed unit | §2, §3, §5.5 |
| 16 | The materializer names the staged repositories in the `safe.directory` of its own system git configuration before the install | §10.2 |
| 17 | An enumeration of workspaces passes over a record outside the current shape and reports it by name | §4.2, §13.2 |
| 18 | The detached record keeps its shape, and a boxed detached workspace's Claude session does not survive the move to `/tree` | §4.2 |
| 19 | A staged pin holds its commit's whole history and the tags within it, where decision 7 first had one commit, so a version derived from tags survives | §10.1 |

## 17. Assumptions and evidence

- **Cold sync** — measured on 2026-11-18 in a boxed session of this repository at `6f175d19` with uv 0.13.1, a fresh `UV_CACHE_DIR` and a fresh environment:
  6.8 s, then 5.4 s with the cache kept and the environment removed.
  The review of this design measured the cold sync again the same way at 6.7 s, the environment at 294 MB and the cache the sync fills at 293 MB.
- **Toolset** — computed with §9.1's rules over this checkout's development environment at `6f175d19`:
  116 installed, 18 roots, 98 in the toolset and the 18 listed in §9.1 left out.
  Without the active-extra rule the OpenAI SDK, `boto3` and their dependencies would also be left out, which is why the rule exists.
  The review recomputed it over that environment and over the container materialization of a bundle frozen from this repository's checkout, with the same result for both.
- **Redirect** — verified on 2026-11-18 with uv 0.13.1 and a cold cache:
  `uv pip install --no-deps "examplepkg @ git+https://github.com/<nonexistent owner>/<repository>@<commit>#subdirectory=sub"`,
  with `GIT_CONFIG_GLOBAL` naming an `insteadOf` rule to a read-only one-commit bare repository, installed the package,
  and its `direct_url.json` recorded the original URL and commit.
  The same install through `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_0` and `GIT_CONFIG_VALUE_0`, with a global git configuration present, installed it too while git still read that configuration.
  #1336's session verified the same redirect for `uv sync --frozen`.
  A pin spelled `git+ssh://git@github.com/…` reaches git as `ssh://git@github.com/…`, and the same install through a redirect of that URL installed it and recorded it.
- **Ownership** — verified on 2026-11-18 with uv 0.13.1 and git 2.47.3,
  `GIT_TEST_ASSUME_DIFFERENT_OWNER=1` standing in for a staged repository that another user owns:
  the install through the redirect fails with git's `detected dubious ownership in repository` naming the staged repository,
  fails the same way with a `safe.directory` entry for that repository among the `GIT_CONFIG_COUNT` variables,
  and succeeds with the entry in a global configuration file.
  A plain `git fetch` from the staged repository succeeds likewise with the entry in a system configuration file.
- **Staged history** — verified on 2026-11-18 with uv 0.13.1 on a two-commit package that takes its version from setuptools-scm, tagged `v1.0.0` at the parent of the pinned commit:
  installed from the original repository its version is `1.0.1.dev1+g<commit>`, from a copy of the one commit `0.1.dev1+g<commit>`,
  and from a copy fetched as §10.1 says `1.0.1.dev1+g<commit>` again, each install recording the original URL and commit.
  uv itself fetches such a pin as `git fetch --force --update-head-ok <url> +<commit>:refs/commit/<commit>`.
  For this repository at `6f175d19` the staged pin is 11 MB, where a copy of the one commit is 2.6 MB.
  A copy holding the history's commits without their files is 1.2 MB larger than the one commit,
  but git refuses to serve it to the ordinary fetch the installer makes.
- **Fetch by commit id** — verified with git 2.47.3 against a `file://` remote:
  `git fetch <url> +<commit>:refs/commit/<commit>` of a commit no ref names succeeds under protocol version 2,
  and fails under version 0 with `Server does not allow request for unadvertised object`.
- **Installations** — read on 2026-11-18: the owner's host holds this checkout and three consumer repositories that pin the framework from git, all four sharing one runtime root.
- **Unverified** — the materializer's `safe.directory` step has not run on a Docker daemon, where root really reads a repository another user owns;
  the route of §14 covers it.
