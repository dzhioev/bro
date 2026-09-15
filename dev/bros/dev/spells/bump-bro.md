---
name: bump-bro
description:

This spell should be used when the user asks to take a newer framework revision into a repository that pins the bro framework from git
— "[[bump bro]]", "bump the framework", "update bro", "take the latest bro", "move the bro pin", "bump bro to <ref>".
Moves the framework pins in the repository's lock and syncs,
reads the range the move takes as commits and changed files,
cross-checks every changed framework surface against the repository's call sites into it
— sourced shell libraries, commands, declaration strings the type checker cannot see, declarative schemas, copied files, inherited prompts —
and adapts what no longer matches,
reads the range for abilities and fixes that replace the repository's workarounds and adopts them,
runs the repository's gate,
commits the bump with the range as its body,
and hands off to [[run pr]].
The framework checkout itself has nothing to bump.

parameters: {"to?": "framework ref to pin — a commit, branch, or tag; default: the head of the branch the sources name"}
version: 1.0.0
---

# bump bro

Take a newer framework revision into a consumer repository and leave the repository working on it.
The lock move is one command;
the work is reading what the range changed and holding the repository to it
— the surfaces a type checker cannot see are where a bump breaks silently
— and taking what the range makes possible.

## Arguments

Passed values appear in the `# Arguments` section appended by the spell tool:

- `to` — the framework ref to pin:
  a commit, a branch, or a tag.
  Default is the head of the branch the sources name.
  Below, `<new>` is the commit the move lands on.

## Step 1 — locate the pin and move it

The pin is in the `pyproject.toml` whose `[tool.uv.sources]` resolves `bro` from a git URL
— the repository root, or a `bro_project/` sidecar
— and the `uv.lock` beside it.
Every `uv` command below runs against that project (`--project bro_project` for a sidecar).
Read the framework packages off the lock before touching it:

```bash
python3 - <<'PY'
import tomllib
for package in tomllib.load(open('uv.lock', 'rb'))['package']:
  git = package.get('source', {}).get('git')
  if git is not None:
    print(package['name'], git)
PY
```

Each framework package's source is the framework URL (up to its `?`) with the pinned commit as its fragment;
the packages sharing that URL are the set to move, and the fragment is `<old>`.
A lock where `bro` has no git source is the framework checkout itself:
there is nothing to bump, report that and stop.

Move the pin:

- default: the sources name a branch, and the move is one `uv lock --upgrade-package <package>` naming every framework package, taking them to that branch's head.
  Sources pinned by `rev` or `tag` move nowhere without `to`;
  say so and ask for the ref.
- with `to`: write it into every framework entry of `[tool.uv.sources]`
  — `branch = "<to>"` for a branch, `rev = "<to>"` for a commit or tag
  — in place of the key that was there, then run the same lock command.

Re-read the lock the same way:
every framework package resolves from the same URL at one identical commit, `<new>`.
Mixed commits or a package left at `<old>` is an error to fix before going on, not a state to work from.
`<new>` equal to `<old>` means the pin was already there;
report it and stop.

Then sync the project the way its own setup does
— its `setup.sh` or docs name the groups and extras.

## Step 2 — materialize the range

The cross-check needs the framework's files on both sides of the move, so read it from a clone rather than an API's file list:

```bash
git clone --filter=blob:none <framework-url> <scratch>/bro
git -C <scratch>/bro merge-base --is-ancestor <old> <new>
```

`<scratch>` is a directory outside the repository
— the session's scratch directory where the harness names one, `mktemp -d` otherwise.
The ancestry check is what makes the move a bump:
`<new>` has to descend from `<old>`, or `<old>..<new>` omits exactly the commits the move removes and step 4 reads a retired ability as a new one.
A downgrade or a ref off another line of history fails it;
stop there and report the two commits, since taking such a move is a different procedure from this one.

Then read the range:

```bash
git -C <scratch>/bro log --format='%h %s%n%b' <old>..<new>
git -C <scratch>/bro diff --name-status <old> <new>
```

Read the log whole:
subjects and bodies say what changed and why, and the `Task:` lines name the tasks the commits closed.
A range of hundreds of commits is still read whole
— skimming a range is how a contract change gets taken unadapted.

## Step 3 — cross-check changed surfaces against the repository's call sites

The repository's type check and test suite cover what they import and execute, and nothing else.
A contract change on any other surface passes a green gate and fails in production, so this step is the gate for those surfaces:
for every changed file, decide how the repository reaches it, grep the repository for that reach, and where the grep hits, diff the reached thing whole and read what each caller consumes of it.
A contract is everything a caller can observe
— arguments, output, exit status, the environment read, the files written, the side effects
— and a caller relying on any of them is as invisible to the gate as one passing arguments.

Classify each changed file by its reach:

- **Sourced shell libraries** — the shell files packaged for consumers (`bro/setup/*.sh` under `bro-shell-dir`, `oops/bro/oops/infra/*` under `bro-oops-dir`, and whatever else the range's docs name as sourceable).
  Diff each changed function whole between `<old>` and `<new>`
  — its arguments and `usage:` line, what it prints, the statuses it exits with, the variables it reads, what it writes or runs
  — and read every call in the repository's scripts for what it consumes of that:
  the arguments it passes, the output it captures or parses, the status it branches on, the environment it sets first, the files it expects afterwards.
- **Commands** — a member's `[project.scripts]` or `bro.session_commands` table, or the module behind a command.
  Grep the repository for the command name in scripts, CI workflows, git hooks, docs, `sh(...)` tool declarations, and deploy-target commands, and read each use the same way:
  the flags it passes, the output it consumes, the exit status it relies on, the environment and files it assumes.
- **Declaration strings** — the persona API (`bro/bro.py`, `bro/mcp.py`, `bros/dev/__init__.py`, `bro/datasources/references.py`, `bro/harness/*`).
  The repository's type check holds its imports and signatures;
  the strings are yours:
  `man('<topic>')` topics, `sh('<command>')` commands, `mount(toolset, '<tool>')` names, feature names, `[[…]]` spell markers in prompt text, and `{{…}}` directive vocabulary.
  Read each string-keyed call in the repository's persona modules against the new declaration.
- **Declarative schemas** — what the framework reads as data:
  `[tool.bro]` and its sub-tables (`bro/workspace/project.py` and each section's owner),
  the host config (`bro/base/host_config.py`),
  credential kinds and material shapes (`bro/base/registry.json`, `bro/setup/AGENTS.md`),
  the entry-point groups the repository contributes to (root `AGENTS.md`, "Extension entry points"),
  and the operations registry (`bro/oops/targets.py`).
  Hold the repository's `pyproject.toml`, entry points, registry module, and credential material shapes against the new schema.
- **Copied files** — a file the repository holds as a copy of a packaged pattern rather than reading it from the wheel (a CodeBuild buildspec, a workflow, a Dockerfile fragment).
  Diff the copy against the file at `<new>`;
  what changed upstream is what the copy is missing.
- **Inherited prompts and spells** — `bro/prompts/**`, `bros/*/spells/*.md`, and the system prompts of the personas the repository's bros derive from.
  A changed instruction a persona inherits may now contradict or duplicate the repository's own prompt text and repository instructions;
  read the diff against them.
- **Everything else** — internal Python and the framework's own tests and docs.
  The repository's gate covers what it imports;
  nothing further to check by hand.

A file can sit in two classes
— a module that is both a command and an import
— and gets both reads.
Each mismatch is adapted in this bump:
the bump is what breaks the call site, so the adaptation belongs with the pin move, not to a later session.
Keep a list of what you adapted and the contract change behind each;
step 6 puts it in the commit body.

## Step 4 — adopt what the range makes possible

A repository accumulates work around what the framework lacked:
a script re-implementing what the framework did not ship, prompt text steering a persona around a bug, a copied file patched where the packaged one fell short, a flag or a revoke dodging a behavior since fixed.
A bump is the moment those become removable, and nothing else prompts it
— so look for them every time, and adopt wherever the replacement is clean.

Read the range for abilities and fixes with the log from step 2 and the docs diff:

```bash
git -C <scratch>/bro diff <old> <new> -- README.md '*AGENTS.md' 'bro/reference/*.md' BOOTSTRAP.md
```

Then read the repository for what it does around their absence:
comments and docs naming a framework limitation or bug, persona prompt text instructing around one, scripts and helpers whose job the framework now does, copied files whose upstream now covers the case, and launch settings set to avoid a behavior.
Pair them:
each ability or fix that replaces something the repository holds is a candidate, with what it replaces and roughly how much changes.

Then decide:

- where questions reach the user, put the candidates to them as the set you intend to adopt, each with what it replaces, and adopt what they confirm;
- unattended, adopt the one-for-one replacements
  — the workaround comes out and the framework's own thing goes in, nothing else moves
  — and leave the larger ones as tasks{{iff #features contains brog}} (`brog::create_task`, one per candidate, naming the ability and what it replaces){{else}} named in the report{{end}}.

Each adoption is its own commit (step 6):
a reviewer judges it apart from the bump, and a wrong one reverts alone.

## Step 5 — gate

Run the repository's formatter, then a persona construction smoke through the project environment
— `uv run bro show <default bro>` for the `[tool.bro] default`, and `uv run bro list`
— which resolves entry points and loads spells and declarations against the new framework, so an import or declaration error surfaces before the suite does.
Then run the repository's own gate (its docs name the command).{{when #harness = bro}}
Run it with an explicit large `timeout_seconds`
— `dev::bash`'s default kills a gate run mid-call.{{end}}

Deploy scripts are exercised by none of this;
step 3 is their gate.
A red gate is fixed here, in this bump:
a failure the pin move causes is the bump's to adapt, and one it does not cause is reported to the user rather than worked around.

## Step 6 — commit and hand off

Two kinds of commit, each in the repository's own conventions ([[run pr]]'s steps 5-6 for the message, the task metadata, and staging by path):

- **The bump**:
  the pin move (the sources where they changed, the lock) together with every adaptation step 3 forced, so the tree works at this commit.
  The body is the range summary
  — one line per commit taken, `<short sha> <subject>`, oldest first
  — followed by each adaptation and the contract change that forced it.
- **Each adoption** from step 4 on its own, its body naming the ability or fix and what it replaced.

Before each commit, run [[run pr]]'s step 2 policy audit as that commit's own precondition:
call `dev-style-source::read`, audit the commit's diff against the returned text, and state the verdict as visible output before the `git commit`.
The hand-off comes after these commits exist, so its audit cannot stand in for this one.

The split is decided here, and [[run pr]]'s fold keeps it:
the bump and each adoption reach the base branch as the commits they are, so a reviewer judges each apart and a wrong adoption reverts alone.

Then [[run pr]]
— it owns the rest, and chains into [[land]] on approval.
Cast it in the response that closes the commits;
the hand-off is this flow's terminal step, not a decision to put back to the user.{{when #features contains brog}}

Where the session has a task, record the bump on it before the hand-off:
`brog::add_comment(<task-id>, topic='bump', body='<old short sha> → <new short sha>, <n> commits; adapted: …; adopted: …')`.{{end}}
