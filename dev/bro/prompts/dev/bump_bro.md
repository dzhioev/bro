# Bump bro

The bro framework, pinned from git by a repository built on it.
The framework checkout itself pins no framework to bump.

## Step 1 — the pin

The pin is in the tracked `pyproject.toml` whose `[tool.uv.sources]` resolves `bro` from a git URL, wherever it sits,
and in the `uv.lock` of its workspace
— beside it, unless it is a workspace member.
Below, `<project>` is that file's directory, and every `uv` command runs against it with `--project <project>`.
Find it and read the framework packages off its lock before touching either:

```bash
python3 - <<'PY'
import subprocess
import tomllib
from pathlib import Path

sources = {}
for path in subprocess.check_output(['git', 'ls-files', '-z', '--', 'pyproject.toml', '*/pyproject.toml'], text=True).split('\0')[:-1]:
  source = tomllib.loads(Path(path).read_text()).get('tool', {}).get('uv', {}).get('sources', {}).get('bro')
  if source is not None:
    sources[str(Path(path).parent)] = source if isinstance(source, list) else [source]
pinning = [project for project, entries in sources.items() if any('git' in entry for entry in entries)]
if len(pinning) != 1:
  raise SystemExit(f'{len(pinning)} projects resolve bro from git; bro sources: {sources}')
workspace = subprocess.check_output(['uv', 'workspace', 'dir', '--project', pinning[0]], text=True).strip()
lock = Path(workspace, 'uv.lock')
print('project', pinning[0])
print('lock', lock)
for package in tomllib.loads(lock.read_text())['package']:
  git = package.get('source', {}).get('git')
  if git is not None:
    print(package['name'], git)
PY
```

It prints `<project>`, its lock, and every package the lock takes from git.
A repository with no such project or with several stops it instead, naming every project's `bro` source;
report those and stop too.
None is the framework checkout itself or a repository taking `bro` from a path or an index, with no git pin to move;
several leave the pin for the user to settle.

Each framework package's source is the framework URL (up to its `?`) with the pinned commit as its fragment;
the packages sharing that URL are the one dependency to move, and the fragment is `<old>`.

Move the pin:

- default: the sources name a branch, and the move is one `uv lock --project <project> --upgrade-package <package>` naming every framework package, taking them to that branch's head.
  Sources pinned by `rev` or `tag` follow no channel.
- with `to`: write it into every framework entry of `[tool.uv.sources]` in `<project>/pyproject.toml`
  — `branch = "<to>"` for a branch, `rev = "<to>"` for a commit or tag
  — in place of the key that was there, then run the same lock command.

Re-run the snippet:
every framework package resolves from the same URL at one identical commit, `<new>`.
Whether `<new>` descends from `<old>` is checked in step 2, on the clone.

## Step 2 — the range

The range is the framework's commits, read from a clone:

```bash
git clone --filter=blob:none <framework-url> <scratch>/bro
git -C <scratch>/bro merge-base --is-ancestor <old> <new>
git -C <scratch>/bro log --format='%h %s%n%b' <old>..<new>
git -C <scratch>/bro diff --name-status <old> <new>
```

A failed ancestry check is a move off the line of history, which stops the bump.
The log's subjects and bodies say what changed and why, and the `Task:` lines name the tasks the commits closed;
step 3 needs the framework's files on both sides of the move, which the clone holds.

## Step 3 — the surfaces

Classify each changed file by how the repository reaches it:

- **Sourced shell libraries** — the shell files packaged for consumers (`bro/setup/*.sh` under `bro-shell-dir`, `oops/bro/oops/infra/*` under `bro-oops-dir`, and whatever else the range's docs name as sourceable).
  Diff each changed function whole between `<old>` and `<new>`
  — its arguments and `usage:` line, what it prints, the statuses it exits with, the variables it reads, what it writes or runs
  — and read every call in the repository's scripts for what it consumes of that:
  the arguments it passes, the output it captures or parses, the status it branches on, the environment it sets first, the files it expects afterwards.
- **Commands** — a member's `[project.scripts]` or `bro.session_commands` table, or the module behind a command.
  Grep the repository for the command name in scripts, CI workflows, git hooks, docs, `cli(...)` tool declarations, and deploy-target commands, and read each use the same way:
  the flags it passes, the output it consumes, the exit status it relies on, the environment and files it assumes.
- **Declaration strings** — the persona API (`bro/bro.py`, `bro/mcp.py`, `bros/dev/dev.py`, `bro/datasources/references.py`, `bro/harness/*`).
  The repository's type check holds its imports and signatures;
  the strings are yours:
  `cli('<command>')` commands, `mount(toolset, '<tool>')` names, feature names, `[[…]]` spell markers in prompt text, and `{{…}}` directive vocabulary.
  Read each string-keyed call in the repository's persona modules against the new declaration.
- **Declarative schemas** — what the framework reads as data:
  `[tool.bro]` and its sub-tables (`bro/workspace/project.py` and each section's owner),
  the host config (`bro/base/host_config.py`),
  credential kinds and material shapes (`bro/base/registry.json`, `bro/setup/AGENTS.md`),
  the entry-point groups the repository contributes to (`bro/reference/extending.md`, "Entry-point groups"),
  and the operations registry (`bro/oops/targets.py`).
  Hold the repository's `pyproject.toml`, entry points, registry module, and credential material shapes against the new schema.
- **Copied files** — a file the repository holds as a copy of a packaged pattern rather than reading it from the wheel (a CodeBuild buildspec, a workflow, a Dockerfile fragment).
- **Inherited prompts and spells** — `bro/prompts/**`, `bros/*/spells/*.md`, and the system prompts of the personas the repository's bros derive from.
  A changed instruction a persona inherits may now contradict or duplicate the repository's own prompt text and repository instructions;
  read the diff against them.
- **Everything else** — internal Python and the framework's own tests and docs, which the repository's gate covers where it imports them.

## Step 4 — the docs

The range's abilities and fixes are in its log and in its docs diff:

```bash
git -C <scratch>/bro diff <old> <new> -- README.md '*AGENTS.md' 'bro/reference/*.md' BOOTSTRAP.md
```

## Step 5 — the smoke

A persona construction smoke through the project environment
— `uv run --project <project> bro show <default bro>` for the installation's `default-bro`, and `uv run --project <project> bro list`
— resolves entry points and loads spells and declarations against the new framework.
Deploy scripts are exercised by none of this or the gate;
step 3 is their gate.

## Step 6 — the commit

The bump's range summary is one line per commit taken, `<short sha> <subject>`, oldest first;
its task comment names `<old>` and `<new>` by short sha and counts commits.
