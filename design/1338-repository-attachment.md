# Design: repository attachment, provisioning and the runtime toolset

This file is the specification of [#1338](https://github.com/dzhioev/bro/issues/1338).
It lives on the branch `design/1338-repository-attachment`, which is never merged:
the pull request that carries it is a design review only.

## 1. Goal, scope and constraints

Goal: a ride workspace is the bro's own directory, and a repository is one optional thing in it.
An attached repository is cloned into a subdirectory of the workspace, describes itself in a language-neutral `bro.toml`, and is provisioned by the session itself, under the session's own identity.
Every session, boxed or unboxed, runs on the installed bro toolset alone, frozen once per root and installed for the host and for containers the same way:
the launcher fetches every git dependency, so an install inside a container needs no credential.

What it enables, for example:

- A joined reviewer writes an oversized round report beside the shared clone and mints it from there, never writing to the clone.
- A repository whose `setup.sh` is a host installer is attached without it running, and a repository that wants provisioning names its command, whatever its file is called.
- A project that pins a private git dependency launches boxed:
  the session's own `github` identity installs it into the project's environment, and a private pin of the toolset reaches the runtime volume through the launcher.
- A non-Python repository declares its provisioning command.
- Upgrading one of the project's development tools, its linter say, no longer creates a new runtime bundle.

The owner settled the design's decisions on 2026-11-18;
§15 records each one and the section that applies it.

Out of scope, each with where it belongs:

- launch configuration, which stays the home settings' and the host configuration's (`bro/reference/ride.md`, "Home settings"):
  a repository never contributes launch configuration;
- a provisioning hook for workspace creation alone, since one command at every start covers the framework's consumers;
- a package cache shared between boxed workspaces, which §8.3 rules out;
- private package indexes, authenticated archive pins and the submodules of a git pin as sources of the runtime volume (§10.3);
- the benchmark's relocatable bundle, which its host builds from the workspace (`benchmark/AGENTS.md`);
- migrating workspaces recorded in today's layout, which are refused instead (§4.3).

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

- **Workspace** — `<runtime-root>/workspaces/<name>/`, a globally named directory holding a writable tree and the records the launch machinery keeps about it.
- **Workspace record** — the workspace's `workspace.json`, written once at creation.
- **Isolation** — `boxed`, in a container, or `unboxed`, on the launcher's filesystem, fixed at creation.
- **Launcher** — the `ride` process that prepares a workspace and supervises its session;
  its **ambient git authentication** is whatever the launching user's own git configuration supplies.
- **`do-ride`** — the session executable every launcher runs inside a prepared workspace.
- **Joined member** — a session that joined another session's party and shares its workspace.
- **Runtime bundle** — the frozen, content-addressed snapshot of the installation that invoked `ride`:
  a manifest of **pins**, `name==version` for an index installation and `name @ git+<url>@<commit>` for a git one, and the wheels of its **local sources**, named by its hash.
  Its **bundle root** is the directory it persists in, `<runtime-root>/runtime/<hash>/`.
- **Host materialization** — the bundle installed into `host/venv` on the launcher, which the launcher re-executes from.
- **Runtime volume** — the Docker volume `ride-runtime-<hash>`, mounted read-only at `/var/ride/runtime` in every boxed session of that bundle;
  the **materializer** is the throwaway runtime-image container that installs the bundle into it, the **container materialization**.
  An unboxed session runs the host materialization, a boxed one the runtime volume, both of its root's one bundle.

Terms this design introduces or redefines:

- **Workspace tree** — the workspace's writable directory, `tree/` on the launcher and `/workspace` in a container (§3).
  It is no longer the clone.
- **Attachment** — the repository a workspace works on:
  its identity, a checkout path or a normalized git URL as `--repo` resolves it, its branch, and its clone name (§4).
- **Clone** — the attachment's independent git clone, a directory directly inside the workspace tree named by the **clone name** (§4.2).
- **Start directory** — the directory a session starts in:
  the clone when the workspace is attached, the workspace tree otherwise (§3).
- **Repository configuration** — the file `bro.toml` at a repository's root (§6).
- **Provisioning command** — the shell command a repository configuration declares to make its clone ready for work (§6.1).
- **Provisioning** — what `do-ride` does to the clone before the harness starts:
  the provisioning command, then the persona's provisioning steps (§7).
- **Toolset** — the installed distributions that plug into bro, the **toolset roots**, together with everything they depend on (§9).
- **Active extra** — an optional-dependency group of an installed distribution whose requirements the installation satisfies (§9.1).
- **Staged pin** — a one-commit bare git repository the launcher fetches for a git pin before it materializes a bundle, for the host or for a container (§10).
- **Wire path** — a path a session names to its host in a request, absolute as the session sees it (§11.1).

## 3. Workspace layout

```
<runtime-root>/workspaces/<name>/
  workspace.json       the workspace record (§4)
  tree/                the workspace tree, /workspace in a container
    <clone name>/      the clone, present only when attached
  lock, exit, resume.json, session.log, session/, <harness>/, party/
                       the records `bro/reference/ride.md`, "Workspaces", describes
```

The workspace tree belongs to the session:
ride creates it empty, places the clone in it on an attached workspace's first launch, and writes nothing else into it.
What a session writes beside the clone stays in the workspace tree, inside the session's artifact reach (§11.2) and outside every git repository.

A session starts in its start directory, in both isolations:

| Workspace | Boxed start directory | Unboxed start directory |
|---|---|---|
| attached, clone name `bro` | `/workspace/bro` | `<runtime-root>/workspaces/<name>/tree/bro` |
| detached | `/workspace` | `<runtime-root>/workspaces/<name>/tree` |
| detached, `--tree PATH` | — | `PATH` |

A joined member starts in its party's start directory, and `ride exec` enters a running boxed workspace there too.
An external tree (`--tree`) still belongs only to a detached unboxed workspace.

For example, `ride along --repo ~/bro -w fix-12 bro-dev` creates the clone at `<runtime-root>/workspaces/fix-12/tree/bro/` and starts the session in `/workspace/bro`;
a reviewer joining that party writes its notes to `/workspace/review/` and never touches `/workspace/bro`.
`ride along -w notes bro` starts in an empty `/workspace`.

## 4. The workspace record

### 4.1 Shape

An attached workspace's record:

```json
{"isolation": "boxed", "attachment": {"repo": "/home/me/bro", "branch": "workspace-fix-12", "clone": "bro"}, "throwaway": false, "tree": null}
```

A detached one:

```json
{"isolation": "boxed", "throwaway": false, "tree": null}
```

- **`attachment`** — present exactly when the workspace is attached, holding three non-empty strings:
  - `repo` — the attachment's identity, the resolved checkout path or normalized git URL;
  - `branch` — the clone's branch, `workspace-<name>` for a new workspace;
  - `clone` — the clone name (§4.2).
- `isolation`, `throwaway` and `tree` keep their meanings (`bro/reference/ride.md`, "Workspaces").

A launch naming an existing workspace with an attachment whose identity differs from the recorded `attachment.repo`, or with one where the record has none, or with none where it has one, is refused, as reusing a name with a different `repo` is today.

### 4.2 The clone name

The launcher derives the clone name once, when it creates an attached workspace, and records it;
every later reader takes it from the record.

- A checkout path's clone name is its last component:
  `/home/me/bro` gives `bro`.
- A URL's clone name is the last segment of its normalized path, with a trailing `.git` removed:
  `https://github.com/dzhioev/bro` gives `bro`, and `git@github.com:acme/widget.git` gives `widget`.

A derived name that is empty, `.` or `..` refuses the launch, naming the attachment.
A started or manual child derives its clone name from the same attachment, so it is its root's.

### 4.3 Records in today's layout

A record carrying top-level `repo` and `branch` is not in the current shape, so every command that reads it refuses the workspace by name and points to `ride clean --force <name>` (`bro/reference/ride.md`, "Runtime state").
Its directory is left as it is, so its files stay readable for recovery.
Today's detached record is already in the current shape.
A kept attached workspace therefore lands or pushes its work before the host's installations move to this design (§13).

## 5. Attachment

### 5.1 The component

Attachment is one component of `bro-ride`, and the rest of ride reaches a repository only through it.
It owns:

- resolving `--repo` to an identity, and the managed URL mirrors (`bro/reference/ride.md`, "Managed URL mirrors"), unchanged;
- the bases a clone is created at (§5.3);
- the record's `attachment` (§4);
- preparing the clone (§5.2);
- the session facts an attachment contributes (§5.4);
- what an attachment changes in a workspace's lifecycle (§5.5);
- the human's git identity, read from the attachment's own git configuration (`ride/ride/identity.py`), unchanged.

The neutral launcher, the workspace model, the container machinery and the bro worker's lowering handle a workspace with or without an attachment, and run no git operation of their own.
What a session does to its clone at start is provisioning, which is `do-ride`'s (§7);
the repository configuration's reader is core's (§6.2).

### 5.2 Preparing the clone

On an attached workspace's first launch, before its container is created when boxed or its session process starts when unboxed, the launcher creates the clone at `tree/<clone name>/` with today's clone machinery and its ambient git authentication:
an independent clone of the checkout or managed mirror, with `origin` retargeted upstream and the attachment's `refs/remotes/origin/*`,
the recorded branch at the resolved base, the initialized submodules, and no alternates (`bro/reference/ride.md`, "Boxed isolation").
It publishes the clone atomically from a temporary sibling.
The clone exists when `tree/<clone name>/.git` is a directory;
a `tree/<clone name>` that exists without one refuses the launch.
Later launches leave the clone exactly as the session left it.

### 5.3 Bases

The base is the commit a new clone's branch starts at;
a launch uses it only to create the clone.
The rules are today's:

- a root bases on `--into` when given, otherwise on a URL attachment's freshly fetched `origin/HEAD` or a checkout's `HEAD`;
- a started child bases on its request's `into` when given, otherwise on the `HEAD` of its summoner's clone, read at summon time
  — the clone of the workspace the summoner runs in, its party's for a joined summoner —
  after transferring into the attachment the commits it lacks, as `bro.workspace.git.resolve_head` does;
- a manual child bases the same way, its pending record naming the summoner's clone;
- a detached root starts detached children and refuses `into`.

### 5.4 Session facts

The launcher of an attached session:

- starts it in the clone (§3), as the container's working directory or the process's `cwd` and `PWD`;
- sets `RIDE_REPO`, `RIDE_REPO_URL`, `RIDE_BRANCH` and `RIDE_BASE_SHA`, and the human's `BRO_HUMAN_NAME` and `BRO_HUMAN_EMAIL`, with today's meanings,
  reading `RIDE_REPO_URL` and `RIDE_BASE_SHA` from the clone where today they are read from the tree.

`RIDE_HOST_WORKSPACE` keeps naming the workspace tree's launcher-side path, attached or not.

A harness keys the state it keeps per working directory to the start directory:
the Claude harness's project directory and trusted path name `/workspace/bro` in the example of §3, and `/workspace` only for a detached boxed session.
Every container's entrypoint marks every directory safe for git (`safe.directory=*`), so it reads no attachment fact.

### 5.5 Lifecycle

- An attached workspace is clean only when its last session exited `0`, and a detached managed tree only while it is empty, as today.
- `ride list` shows `attachment.repo` as the workspace's attachment.
- `ride clean` keeps a managed mirror while some record's `attachment.repo` names its URL, and removes an attached workspace whose checkout or mirror is gone only with `--force`, as today.
- The launch configuration a root reads depends on no attachment:
  the reading stops carrying the attachment identities and repository sections it holds today, which no rule reads (`ride/ride/authority_reading.py`), and `ride scope` drops `--repo`.

## 6. Repository configuration

### 6.1 The file

A repository describes itself to bro in `bro.toml` at its root:

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

A repository without `bro.toml` has no repository configuration.
`[tool.bro]` in `pyproject.toml` is no longer read by anything:
its owner tables move to `bro.toml`, and its `build-context-command` goes with the project image it served (§8.1).

### 6.2 Readers

One reader in `bro.workspace`, replacing `bro/workspace/project.py`, parses and validates the file for every consumer.

- `do-ride` reads `<start directory>/bro.toml` at each attached start, as the clone holds it then, so a commit that changes the file takes effect at the next start.
- `bro-oops` reads `[devoops]` from the file at the root of the repository it runs in, found as today (`bro.workspace.paths.find_project_root`).
- The launcher reads nothing from a repository's content.

## 7. Provisioning

### 7.1 When

Provisioning runs at every start of an attached session that is not a joined member:
a root's launch and each of its resumes, a started child, and a manual child, in both isolations.
A detached session and a joined member run none;
a joined member's party has already provisioned the shared clone.

### 7.2 Order

`do-ride` starts an attached session in this order:

1. the session environment it sets today:
   the bro's git identity, the `RIDE_*` facts, `BRO_HOLD` and `RIDE_RUNNER_PID`;
2. the scoped credential hooks;
3. the provisioning command, when the repository configuration declares one;
4. the persona's provisioning steps (`BaseBro.provision_workspace`), given the clone's root;
5. the harness's session preparation, then its run.

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

This repository declares `provision-command = "./setup.sh"`, whose sync no longer compares a baked environment's manifests.
A plain uv project needs no declaration.

### 7.5 What it replaces

Nothing runs a file by its name any more:
the entrypoint's `setup.sh` call and project-venv link, `RIDE_VENV_MANIFEST`, and the unboxed launcher's own `setup.sh` run (`ride/ride/workspace/worktrees.py`) go.
The entrypoint keeps aligning the `ride` user with the workspace tree's owner, re-owning the scoped store, marking directories safe for git, and executing `do-ride`.

## 8. Images and package caches

### 8.1 One image

Every boxed session runs the runtime image, attached or not.
The project image goes, and with it `project.Dockerfile`, the project build context and its manifests, `build-context-command`, the `ride-attachment` label and the project-image pruning;
removing a workspace's root-owned files uses a present runtime image.
The runtime image's tag stops hashing `project.Dockerfile`, so the landing builds one new runtime image.

### 8.2 The project's environment

What provisioning builds in the clone, such as its `.venv`, lives on the workspace tree's mount, so it persists through the workspace's later sessions and resumes;
a new workspace builds it at its first start.
A boxed session's uv cache stays in its container's own layer, and the runtime image sets `UV_LINK_MODE=copy`, since that cache and the clone lie on different filesystems.

A cold full sync of this repository, `uv sync --frozen --all-packages --all-groups --all-extras`, took 6.8 s in a boxed session, and 5.4 s with a warm cache (§16).

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
The 18 distributions left out are development tools and what only they need (§16):
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
For each distinct URL among the bundle's git pins, it creates a bare repository under `<bundle root>/git-pins/`, fetches each commit the bundle pins from that URL with `git fetch --depth 1 <url> <commit>`, and places a ref at it.
The fetch runs under the launcher's ambient git authentication with prompts disabled, as mirror fetches do.

The URL is the pin's, without its `git+` prefix, its `@<commit>` and its fragment, which is how uv hands it to git.
Every git pin is staged, public or not, since the launcher cannot tell which ones a container could fetch.
A failed fetch fails the materialization, naming the pin and git's message.
A `git-pins/` directory a killed launch left behind is replaced.
A bundle without git pins stages nothing.

### 10.2 Installing through the staged pins

The install runs with one redirect per staged URL added to its git configuration through `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_<n>` and `GIT_CONFIG_VALUE_<n>`:
`url.<staged repository>.insteadOf = <url>`, where the staged repository is a `file://` URL of its path as the install sees it.
The variables add to whatever configuration git reads otherwise rather than replacing it.

- The host materialization's install reaches `<bundle root>/git-pins/` at its own path, over the launching user's git configuration.
- The materializer mounts `<bundle root>/git-pins/` read-only at `/var/ride/git-pins`, and its install has no other git configuration.

The pins themselves are unchanged, and the installed record keeps each pin's original URL and commit (§16).
The launcher removes `<bundle root>/git-pins/` once the materialization ends, whether it succeeded or failed, so nothing staged persists:
the bundle, its hash and both materializations are what they would be without staging.
Every materialization goes this way:
the host's, and the container's for session containers, worker containers and foreground worker runs.

### 10.3 Limits

Inside the materializer, staging covers each git pin's own commit and nothing it needs beyond:

- a staged pin's submodules are fetched from their own URLs, without credentials;
- a distribution from a private package index, and a remote-archive pin behind authentication, are downloaded without credentials.

Each still fails the container materialization with the installer's error, which names the distribution.
The host materialization reaches them through the launching user's own configuration, as today.

## 11. Paths a session names to its host

### 11.1 Wire paths

Every path a session names to its host in a request, an artifact to mint or a benchmark job's files, is a wire path:
absolute, as the session's own processes see it.
The client that builds the request makes a relative path absolute against its working directory, lexically and without resolving symbolic links, and checks nothing else.

The host owns the check.
Its description of each peer carries, beside the peer's tree, the path the peer sees that tree at:
`/workspace` for a peer that runs in a container, which is a boxed session, a joined member of a boxed party or a worker container, and the tree's own path for an unboxed peer.
One core helper, replacing `bro.worker_types.tree_path`, maps a wire path from that view to the tree and resolves it on the host.
It refuses the path, naming it, when it is relative, lies outside the peer's view of its tree, or resolves outside the tree.

### 11.2 Minting artifacts

`artifact mint <path>`, and the client function under it, accept a path relative to the caller's working directory or an absolute one, and send it as a wire path.

From `/workspace/bro` in the workspace `fix-12` of §3:

- `artifact mint report.md` sends `/workspace/bro/report.md`, which the host reads at `<runtime-root>/workspaces/fix-12/tree/bro/report.md`;
- `artifact mint ../review/r3.md` sends `/workspace/review/r3.md`;
- `artifact mint /etc/hosts` reaches the host and is refused;
- a symbolic link inside the tree that points to `/opt/cache` resolves outside the tree on the host and is refused.

The webview mints paths relative to its working directory, its workspace tree, so they keep their meaning.

### 11.3 A joined reviewer's oversized round

The eyebro's `review` spell writes an oversized round to a unique directory in the workspace tree outside the clone, at the workspace path its banner reports, mints it from there, and removes that directory;
the stopgap that stages it under the clone's `.git/review-rounds/` goes.

### 11.4 The benchmark job

`benchmark-run` sends its job's `config` and, in a new `project` field, the benchmark project's directory, both as wire paths;
the job maps both through the helper of §11.1 and refuses a `project` without a `pyproject.toml`.
Its environment stays beside the workspace tree, at `<workspace>/benchmark-venv`.

## 12. Documentation, this repository and its consumers

- `bro/reference/ride.md`:
  the mode verbs' attachment, "Runtime bundles" (§9, §10), "Workspaces" (§3, §4),
  "Attached-tree configuration" rewritten as the repository configuration (§6),
  "Unboxed isolation" and "Boxed isolation" (§5.2, §7.5, §8.1), "Sharing artifacts between peers" (§11.2), and "Forwarded env vars" (the start directory);
  provisioning's contract, its idempotency included, is stated there once.
- `BOOTSTRAP.md`:
  the root-lock image gates and project-image proofs go;
  a repository with a setup flow declares it as `provision-command` in `bro.toml`, replacing the `setup.sh` rule;
  `[tool.bro]` becomes `bro.toml`;
  and the packaging choice states what reaches the toolset (§9.2).
- `bro/reference/extending.md`:
  the `bro.` entry-point namespace defines the toolset roots.
- `bro/artifact.py`'s wire contract:
  `artifact.mint` carries a wire path.
- The maps (`ride/AGENTS.md`, `ride/ride/workspace/AGENTS.md`, `ride/ride/setup/AGENTS.md`, `bro/workspace/AGENTS.md`), `oops/README.md`, and the `[tool.bro]` mentions in `dev/bro/dev/spells/reflect.md` and `dev/bro/prompts/dev/bump_bro.md`.
- This repository:
  `bro.toml` declares `provision-command = "./setup.sh"` and carries `[devoops]`, `pyproject.toml` drops `[tool.bro.devoops]`, and `setup.sh` drops `manifests_match`.
- Consumer repositories move their `[tool.bro]` tables to `bro.toml` and declare their provisioning command in the commit that takes the framework revision.
- Regatta's builder pilot reads a build's result from the clone, now at `tree/<clone name>/` as its workspace record names it;
  `RIDE_BRANCH`, `RIDE_BASE_SHA` and the clone's extra local branch stay as they are.

## 13. Rollout

- A launcher reads only the record shape it writes (§4.3), so an old launcher refuses a new record and a new launcher an old attached record, each by name;
  neither misreads the other.
- Before the host's installations move, every kept attached workspace lands or pushes its work, since it is refused afterwards.
- Project images left on the daemon carry a label no launcher reads any more;
  they are removed by hand, as `docker image rm` of the `bro/ride-project` repository's tags.
- The plan phase settles the stages, their order and the moves of the consumer repositories.

## 14. Verification

Each route below needs automated coverage, the boxed ones in the host-only Docker stage:

- an attached boxed launch and an attached unboxed launch start in the clone, with the workspace tree writable beside it;
- a relative mint from the clone and an absolute one beside it are accepted, while a path outside the tree and a symbolic link out of it are refused by the host;
- a repository declaring a provisioning command runs it at the launch and at a resume, after the credential hooks, and a failing command ends the start before the harness runs;
- a repository with a `setup.sh` and no declaration starts without running it;
- an unknown top-level key in `bro.toml` refuses the start;
- a freeze leaves out a distribution no root reaches, keeps an active extra's requirements, and its bundle reproduces itself;
- a bundle with a git pin whose URL neither the host nor the container can reach materializes for both through its staged pin, and leaves nothing staged;
- an attached record in today's layout is refused by name.

A private dependency fetched through a real `github` credential is verified live, once, by the verification phase.

## 15. Decision record

The owner settled every decision below on 2026-11-18.

| # | Decision | Applied in |
|---|---|---|
| 1 | Attachment stays one self-contained component of `bro-ride` rather than a separate distribution | §5.1 |
| 2 | The clone is a subdirectory of the workspace tree, named after the repository and recorded | §3, §4 |
| 3 | A language-neutral `bro.toml` replaces `[tool.bro]`, carrying `provision-command` and owner tables | §6 |
| 4 | Provisioning runs in `do-ride` after the credential hooks, in both isolations, and fails the start | §7 |
| 5 | The project image and the `setup.sh` filename hook go | §7.5, §8.1 |
| 6 | Boxed workspaces share no package cache | §8.3 |
| 7 | Every materialization, host and container, reaches git pins through launcher-staged one-commit copies, the redirect layered over the git configuration through `GIT_CONFIG_COUNT` | §10 |
| 8 | A runtime bundle carries the bro toolset rather than the whole invoking installation | §9 |
| 9 | Workspaces recorded in today's layout are refused, not migrated | §4.3, §13 |
| 10 | Every session, boxed or unboxed, runs its root's one bundle | §1, §2 |
| 11 | A path a session names to its host travels absolute; the client makes it absolute and the host maps and checks it | §11.1 |

## 16. Assumptions and evidence

- **Cold sync** — measured on 2026-11-18 in a boxed session of this repository at `6f175d19` with uv 0.13.1, a fresh `UV_CACHE_DIR` and a fresh environment:
  6.8 s, then 5.4 s with the cache kept and the environment removed.
- **Toolset** — computed with §9.1's rules over this checkout's development environment at `6f175d19`:
  116 installed, 18 roots, 98 in the toolset and the 18 listed in §9.1 left out.
  Without the active-extra rule the OpenAI SDK, `boto3` and their dependencies would also be left out, which is why the rule exists.
- **Redirect** — verified on 2026-11-18 with uv 0.13.1 and a cold cache:
  `uv pip install --no-deps "examplepkg @ git+https://github.com/<nonexistent owner>/<repository>@<commit>#subdirectory=sub"`,
  with `GIT_CONFIG_GLOBAL` naming an `insteadOf` rule to a read-only one-commit bare repository, installed the package,
  and its `direct_url.json` recorded the original URL and commit.
  The same install through `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_0` and `GIT_CONFIG_VALUE_0`, with a global git configuration present, installed it too while git still read that configuration.
  #1336's session verified the same redirect for `uv sync --frozen`.
- **Unverified** — a git pin spelled `git+ssh://` reaches git as `ssh://…` and the same mapping applies;
  the implementation's test covers it.
