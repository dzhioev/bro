# Bootstrap a repository with bro

You are the attended development-agent session responsible for adopting the bro framework in a repository chosen by the user.
This prompt may run under any harness that can inspect and edit the target on its host.
Treat this file as an executable checklist, not as background documentation.
Work through the numbered sections in order and do not perform the user's choices on their behalf.

The supported outcome is a macOS or Ubuntu host checkout whose activated project-local uv environment can run this exact command successfully, followed by the same command under the other harness when section 3 wired that harness's key:

```console
ride solo my-dev "sup?" --repo .
```

`my-dev` is the suggested public name.
If the user chooses another name, substitute only that name in the acceptance command.
The bootstrapped repository installs both harnesses, Claude Code in full mode and bro's native loop, whichever harness executes this prompt.
Which harness a launch or a summon runs under when nothing names one is the user's configuration choice in section 3;
either harness stays reachable per launch through `--harness` and per summon through the request's `harness` field.
Never recommend `--raw`.
Do not use Anthropic API-key authentication or replace the exact acceptance command with unboxed isolation.

## Operating contract

For every numbered section, follow its **Check**, **Ask**, and **Act** subsections, then report the named checkpoint.
Checks are read-only unless the subsection explicitly says otherwise.
Before each group of writes, show the exact intended action and obtain confirmation for each boundary it crosses:

- repository or target-directory files, staging, and commits;
- files under the user's home directory;
- outward actions such as creating a repository, changing a remote, pushing, opening a pull request, or creating an issue.

A previous confirmation does not authorize a later boundary.
Never stage unrelated files.
Never run a repository's existing setup script before reading its instructions and contents.
Stop on a failed prerequisite or ambiguous state instead of weakening the outcome.

Keep a checkpoint ledger in the conversation.
On rerun, recheck each claimed fact and continue from compatible state rather than appending duplicate dependencies, source declarations, entry points, configuration, credential instances, or tasks.
Show conflicting prior values and ask whether to preserve or replace them.
Do not overwrite them silently.
A restart is safe only at a completed checkpoint or before an approved write group begins.

Never ask the user to paste a credential into chat.
Never print credential contents or place them in repository files, shell history, process arguments, or command output.
Secret capture belongs in a separate user terminal through hidden input or direct stdout redirection with restrictive permissions.
Refer to credentials only by kind, instance name, expected non-secret identity, selection layer, and availability.

## 1. Establish the target and execution boundary

### Check

1. Detect a container through `/.dockerenv`, `/run/.containerenv`, and Linux cgroup markers, then identify the operating system.
2. Locate the current Git root if one exists.
   Inspect `git status --short --branch`, `git remote -v` with credential-bearing URLs redacted, the current branch, and whether `HEAD` exists.
3. Determine the current harness's writable project boundary and how it restarts at a different root.
4. Read the target's root and relevant subsystem `AGENTS.md`, `CLAUDE.md`, packaging files, setup scripts, and repository instructions.
5. Check `git`, Python 3.12 or newer, `uv`, `claude`, and a responding Docker daemon with version or status commands.
6. Inspect whether `BRO_STORE` is set without displaying a value.
   This workflow uses the conventional persistent store at `~/.bro`, so an inherited directed store is a separate execution boundary.
7. If GitHub is under consideration, check `gh --version` and `gh auth status` without printing a token or a credential-bearing remote URL.

### Ask

1. Ask whether to use the current repository, another existing repository, or a new repository.
2. For a new repository, settle its project name, one-sentence purpose, target path, and default branch.
3. Choose the developer bro's public name, defaulting to `my-dev`.
   Derive a collision-free Python module name and class name separately rather than assuming the public name is a valid identifier.
4. Ask whether the repository will live on GitHub, its owner and visibility if it must be created, whether GitHub Issues should be its task tracker, and whether this session may create or change `origin`.
5. Explain that acceptance requires a focused local commit.
   Ask separately whether this session may push that commit or open a setup pull request.

### Act

1. If this session is in a container, stop before any repository or credential write.
   Give the user the current harness's host restart command at the target root with the raw URL for this file.
2. If the target is outside the current writable project boundary, stop before edits.
   Give the equivalent restart command from the target root.
3. If `BRO_STORE` is set, stop host credential work until the user deliberately unsets it or chooses a separate workflow outside this prompt.
4. For a new repository, after approval create the directory, run `git init -b <default-branch>`, add only confirmed starter files, and verify the user's Git identity before a commit.
5. Create a GitHub repository or change `origin` only after showing and receiving approval for the exact outward action.
6. Preserve every unrelated dirty path in an existing repository.

**Checkpoint 1:** report the canonical target root, operating system, writable boundary, Git state, chosen public/module/class names, GitHub and brog choices, delivery permission, tool versions, Docker availability, and that `BRO_STORE` is unset.

## 2. Choose the packaging shape and pin one framework revision

### Check

1. Classify the target as an installable PEP 621 project, a uv workspace, a non-Python repository, or a project owned by another packaging system.
2. Identify the distribution and package-discovery rule that would ship the bro declaration.
3. Inspect the active environment and lock for private, local-only, platform-specific, or otherwise unreproducible distributions.
   `ride` freezes the entire invoking installation, not only bro's dependency closure.
4. Search installed and declared `bro` entry points and existing `[tool.bro]` tables for name or behavior conflicts.
5. Reconcile any partial bootstrap across dependencies, Git sources, entry point, module, lock, project configuration, environment, and ignored files.
6. If the repository root has `uv.lock`, inspect the root and every workspace member, dependency group, extra, source override, and local path dependency.
   The root lock triggers a project image independently of the environment that supplied `ride`.
7. Treat both project-image phases as mandatory:
   the manifest-only phase receives the root lock, root manifest, and discovered workspace-member manifests and runs `uv sync --frozen --all-packages --all-groups --all-extras --no-install-workspace`;
   the full-tree phase receives the complete tracked build context and runs the same command with workspace installation enabled.
8. A non-workspace path source may pass the full-tree phase while failing the manifest-only phase.
   Do not infer support from a successful host sync.

### Ask

1. In an existing uv-managed Python project, ask whether the bro declaration may join its installable distribution or an installed workspace member.
2. Use the integrated shape only when the project's package discovery, complete invoking environment, and both root-lock image phases are reproducible under the runtime image's Linux, Python, network, and no-launcher-credential conditions.
3. Propose an independent `bro_project/` sidecar when the repository uses another package manager, has ambiguous package discovery, or has no root lock and its application environment is too broad.
4. In a viable uv workspace, choose deliberately between an installed member and a sidecar excluded from workspace discovery.
5. Explain that a sidecar narrows only the invoking installation.
   It does not suppress either image phase selected by a root `uv.lock`.
6. If either root-lock phase cannot succeed, stop before claiming support.
   Name the missing project-bake selection or suppression seam as prerequisite framework work.

### Act

For an integrated root or installed workspace member, merge these roles into the owning distribution without replacing existing metadata, ordering, source conventions, or setup commands:

```toml
[project]
dependencies = [
  "bro[github,llm,runtime]",
  "bro-dev",
]

[dependency-groups]
dev = [
  "bro-native",
  "bro-ride",
]

[tool.uv.sources]
bro = { git = "https://github.com/dzhioev/bro", branch = "master" }
bro-dev = { git = "https://github.com/dzhioev/bro", subdirectory = "dev", branch = "master" }
bro-native = { git = "https://github.com/dzhioev/bro", subdirectory = "native", branch = "master" }
bro-ride = { git = "https://github.com/dzhioev/bro", subdirectory = "ride", branch = "master" }
```

Preserve an existing project's narrower setup selection instead of forcing all groups and extras on its ordinary setup command.
The operator group must still install `bro-ride` and `bro-native`:
`ride` spawns the native loop as the `bro` command of the environment it froze, so a native launch or summon fails in a workspace whose environment lacks `bro-native`.
`bro-dev` remains a runtime dependency because the consumer class imports `bros.dev.Dev`.

For a fresh integrated root, use normalized project metadata, version `0.1.0`, the confirmed description, `requires-python = ">=3.12"`, and this packaging shape after substituting the chosen identifiers:

```toml
[build-system]
requires = ["uv_build>=0.12,<0.13"]
build-backend = "uv_build"

[project.entry-points.bro]
my-dev = "bros.my_dev:MyDev"

[tool.uv.build-backend]
module-root = ""
module-name = ["bros.my_dev"]
namespace = true
```

Leave `bros/` without `__init__.py` because it is a shared PEP 420 namespace.
Put the declaration in `bros/my_dev/__init__.py`.
Adapt an existing backend's own package-discovery configuration instead of adding incompatible uv-build settings.

For a sidecar:

1. Keep `[tool.bro]` in the repository-root `pyproject.toml`, creating a table-only root file if necessary.
2. Create `bro_project/pyproject.toml` as a complete Python 3.12 uv-build project named `<project-slug>-bros`.
3. Give it the same framework runtime dependencies and Git sources, with `bro-native` and `bro-ride` in its `dev` group.
4. Register `my-dev = "<project_slug>_bros.my_dev:MyDev"`.
5. Set `module-root = "src"` and `module-name = ["<project_slug>_bros"]`.
6. Create `bro_project/src/<project_slug>_bros/__init__.py` and `my_dev.py`.
7. Derive a valid, collision-free Python package name.
8. Keep the sidecar outside root workspace-member globs, adding a workspace exclusion when needed.
9. Address every sidecar operation with `--project bro_project`.
10. Ignore only `bro_project/.venv/`, build output, and normal Python cache files, and commit `bro_project/uv.lock`.

If a root `setup.sh` exists, preserve its flow and add only the selected sync and `bro.dev.install` operations after reading and obtaining approval.
Use `uv run --project bro_project bro.dev.install` for a sidecar.
Do not add a setup script merely because one is absent.

Show the proposed dependency and source changes, then obtain approval before changing files and before relocking.
Run the selected-project equivalent of:

```console
uv lock --upgrade-package bro --upgrade-package bro-dev --upgrade-package bro-native --upgrade-package bro-ride
```

Parse the resulting `uv.lock` rather than trusting command output.
Require `bro`, `bro-dev`, `bro-native`, and `bro-ride` to resolve from `https://github.com/dzhioev/bro` at one identical 40-character commit SHA.
Reject a retained older revision, mixed revisions, missing Git source, or branch movement during the operation.
Call that exact value `BRO_SHA` for the remainder of this bootstrap.

Read all framework sources from that revision, never from a local stale checkout or moving `master`.
Construct every read as `https://raw.githubusercontent.com/dzhioev/bro/${BRO_SHA}/<path>` or `https://github.com/dzhioev/bro/blob/${BRO_SHA}/<path>` after substituting the actual SHA.
Fetch and read these paths before using their contracts:

- `README.md`;
- `AGENTS.md`, especially **Adding a Bro**;
- `bro/registry.py`;
- `dev/bros/dev/__init__.py`;
- `local/bros/bro_dev/__init__.py`;
- `bro/workspace/project.py`;
- `bro/llm/llms/claude_code.py`;
- `bro/llm/llms/openai.py`;
- `bro/reference/ride.md`, especially **Harness selection**, **Per-project defaults**, **Summoning another bro**, and **Scoped credential hydration**;
- `bro/reference/dive_in.md`;
- `ride/ride/dive_in.py`;
- `ride/ride/bro.py`;
- `bro/setup/AGENTS.md`;
- `bro/base/host_config.py`;
- `bro/brog/system.py`;
- `bro/brog/github.py`;
- `dev/bros/dev/spells/wire.md`;
- `ride/ride/setup/container/project.Dockerfile`;
- `ride/ride/workspace/build_context.py` and `ride/ride/workspace/docker.py`.

If any read fails or the source contradicts this prompt, stop and show the contradiction.
Do not combine instructions from current `master` with an older lock.

For the preliminary root-lock gate, use the pinned `ride.workspace.build_context` rules to prepare two temporary trees without changing the target.
The first contains only `uv.lock`, the root `pyproject.toml`, and every discovered workspace-member `pyproject.toml` at its original relative path.
The second contains the complete tracked build context plus the approved pending packaging files.
Build the computed pinned runtime image if absent, then run each uv command in a fresh container from that image with no launcher credential, credential directory, or host package cache mounted.
Run the manifest tree with `--no-install-workspace` and the full tree without it, using `--frozen --all-packages --all-groups --all-extras` in both.
This is a support gate, not the final image proof; section 4 builds the actual project image after its required `[tool.bro]` settings and declaration exist.
Run both gates against the root even when `ride` comes from `bro_project/.venv`, and do not substitute a sidecar-only sync.
Ask before local Docker image writes, show the image tag and temporary paths, remove the temporary trees afterward, and fail closed on either phase.

**Checkpoint 2:** report the selected packaging shape, owning distribution, exact `BRO_SHA`, all four locked source records, source paths read at that SHA, root-lock status, both image-phase results, and any preserved project setup conventions.

## 3. Choose the harness configuration and write project launch settings

### Check

1. Read the repository-root `[tool.bro]` table and the pinned closed scalar schema.
2. Inspect any existing project default, `harness`, `summon-harness`, image repository, sub-tables, and host `~/.bro.json` LLM preset names without reading secrets.
3. Determine a lowercase Docker-compatible image repository unique among this host's consumer repositories.

### Ask

1. On first adoption, propose the selected developer as `[tool.bro] default`.
2. If an already bro-enabled repository intentionally uses another default, ask whether to preserve it.
   A preserved default requires `--bro <developer-name>` on default-dependent `ride scope` and `dive-in` commands.
3. Explain the two harness keys from the pinned per-project defaults:
   `harness` drives every attached launch that names no `--harness`, which is `dive-in`, `ride along`, and `ride solo` alike, and `summon-harness` drives every summon whose request names no harness.
   Claude Code runs on the `claude_code` setup token, which section 5 always wires;
   the native loop runs the developer's `llm_spec` recipe on its provider key, the `openai` credential.
4. Ask whether the user has an OpenAI API key and wants section 5 to wire it.
   Without one, both keys are `claude`, the native loop stays installed but unusable, and the user changes the keys once a key is wired later;
   skip the next question.
5. Present these configurations and ask which one to write:
   - recommended: `harness = "claude"` and `summon-harness = "bro"`, so interactive and one-shot launches run under Claude Code and summons under the native loop, which is how the framework repository runs itself;
   - Claude Code everywhere: both keys `claude`;
   - native everywhere: both keys `bro`, not recommended because the interactive native harness is early, working but well behind Claude Code's own session;
   - the remaining pair, `harness = "bro"` with `summon-harness = "claude"`, when the user wants it.
6. If an already bro-enabled repository carries intentional `harness` or `summon-harness` values, present them as its current configuration and ask whether to preserve or replace them.
7. Ask whether the pinned Claude Code model and effort defaults are acceptable.
   Explain that a non-default recipe is an opt-in named preset, not an automatic project scalar.

### Act

Merge the approved values into the repository-root table, writing both harness keys explicitly:

```toml
[tool.bro]
default = "my-dev"
harness = "claude"
summon-harness = "bro"
image-repository = "bro/<project-unique-docker-slug>"
```

Substitute the chosen public name and the chosen harness pair consistently.
Preserve an intentional existing default when approved.
Leave `summon-depth`, `build-context-command`, and unrelated sub-tables absent unless a discovered project requirement needs them.
Unknown root scalars are errors.
`--harness` on a `ride` or `dive-in` command and `harness` on a summon request override the keys for one launch;
do not add them to ordinary commands.

If the user rejects the framework's current Claude defaults, choose a project-specific key that does not collide with host `~/.bro.json` and write:

```toml
[tool.bro.llm]
<project-slug>-developer = "claude-code:<model>:<effort>"
```

Validate the model alias and effort against the pinned Claude recipe source.
The current neutral effort vocabulary is read from the pinned source rather than assumed.
Add `--llm <project-slug>-developer` to ordinary `ride` and `dive-in` commands only.
Do not add it to the exact acceptance command, which must continue to test the framework default.

**Checkpoint 3:** report the effective default, the harness pair and the configuration it is, whether an OpenAI key will be wired, unique image repository, whether commands need `--bro`, and the optional named preset and its explicit-use rule.

## 4. Define and package the developer bro

### Check

Ask the user for durable repository facts only:

- the repository purpose;
- where its conventions live;
- formatter, focused test, full test, and package/build commands;
- generated-file rules;
- deployment boundary;
- paths that must never be staged.

Do not paste a large project map into the persona.
Update repository instructions only with confirmed, always-applicable facts.
Read the pinned generic and framework-project developer declarations before writing the class.

### Ask

1. Confirm the short system-prompt addition that names the project and its durable constraints.
2. Confirm whether the developer will use GitHub for pushes and pull requests.
3. Confirm whether GitHub brog will be configured.
4. Explain that `llm_spec` is the recipe the native loop runs the developer on, every launch and summon under the `bro` harness, while a Claude Code session ignores it.
   Ask whether to use the pinned framework developer's current OpenAI model and high reasoning effort or another valid pinned native recipe.
   Declare it even when no OpenAI key is wired yet, so the native loop needs only the key.

### Act

Create the selected module with this shape, adapting names, project text, and conditional lines:

```python
import bro.llm.llms.openai as llm_llms_openai
from bro.datasources.references import man
from bros.dev import Dev

PROJECT_PROMPT = """\
## <project> project

You are operating inside the <project> repository.
Read the root and relevant subsystem `AGENTS.md` files before changing code.
Follow the repository's own formatter, test, and build instructions.
<Only confirmed, always-applicable project constraints go here.>
"""


class MyDev(Dev):
  name = 'my-dev'
  description = '<project> development: task → implement → verify → land'
  llm_spec = llm_llms_openai.LLMSpec(
    model='<model from the pinned source>', reasoning_effort='high'
  )
  features = {'brog': True}
  extra_secrets = ('github',)
  data_sources = [
    man('environment'),
    man('template'),
    man('conditions'),
    man('ride'),
    man('dive-in'),
  ]
  system_prompt = PROJECT_PROMPT
```

Apply these rules:

- Set `features = {'brog': True}` only when this repository's brog is chosen and successfully configured.
- While a newly chosen brog still awaits section 5, leave the value `False` and record the one pending repository edit rather than claiming it works.
- Keep it `False` when brog is declined, so ambient credentials cannot silently enable task workflows.
- Include `extra_secrets = ('github',)` for a GitHub development workflow or GitHub brog.
  GitHub brog's managed identity probe and exhaustive issue scan use `gh`, whose scoped install hook runs only when `github` is a declared kind rather than a transitive `$cred` reference.
- Do not redeclare Dev's tools, style source, spells, or hook provisioning.
- Do not declare `may_summon` before the roster-design task identifies a real collaborator.
- Keep the entry-point key equal to the class's `name`.
- Do not add a `my-dev` alias when the user chose another public name.

Sync again after editing entry-point metadata.
For a dedicated fresh project, run:

```console
uv sync --all-groups --all-extras
uv run python - <<'PY'
from bro.registry import create_bro, declared_specs

expected_name = '<selected-public-name>'
expected_target = '<selected-module>:<selected-class>'
assert declared_specs()[expected_name] == expected_target
assert create_bro(expected_name).name == expected_name
PY
uv run bro.dev.install
uv build
```

Use the established sync and build commands for an existing integrated application while ensuring the operator group is installed.
For a sidecar, add `--project bro_project` to every uv command.
Inspect the built wheel for the bro module and the `bro` entry point so an editable import cannot hide missing wheel contents.
Rerun the one-SHA lock assertion after the final metadata changes.
For a root lock, obtain approval to stage only the intended bootstrap paths so the default tracked build context includes new files.
Then use the pinned selected environment's `ride.workspace.build_context` and `ride.workspace.docker` functions against the repository root.
Build the computed runtime image if absent, assemble the root project context, and build the computed project image.
The pinned `project.Dockerfile` must complete both its manifest-only `--no-install-workspace` layer and its full tracked-tree workspace-installation layer without launcher credentials.
Run this root proof even when the executable and imports come from the sidecar environment.
A repository with no root lock must return no project-image tag; do not manufacture a root bake for it.

**Checkpoint 4:** report the entry-point mapping, class behavior choices, successful installed-registry probe, wheel contents, final one-SHA assertion, and final image-phase results.

## 5. Prepare host credentials and select their instances

### Check

1. Confirm again that this is a host session and `BRO_STORE` is unset.
2. Read the pinned wire spell, setup schema, host-config module, and scoped-credential manual.
3. Run `credentials list` and `credentials list --instance` to inspect kinds and instance names without resolving or printing values.
4. Inspect only safe metadata needed to distinguish existing instances.
5. Read `git remote get-url origin` exactly.
6. Classify the result with the pinned framework's Git URL rules.
   An origin naming a local filesystem path contributes no URL identity, including when that path is relative.
7. If a Git URL embeds a password or token, including either one in HTTPS userinfo, do not display or record it; propose replacing it with an approved credential-free SSH or HTTPS URL first.
   An ordinary SSH username, including the `git@` in a standard SCP-like GitHub remote, is not credential material and must be preserved.
8. Use the exact credential-free Git URL spelling as the portable project identity.
   URL matching preserves path case, `.git`, userinfo, and transport spelling while normalizing only scheme and hostname case plus trailing slashes.
9. Use the canonical checkout path when no origin exists, when origin names a local path, or for a machine-specific override.

### Ask

1. For each needed kind, present existing instances by non-secret identity and ask which one this project should use.
2. Ask whether to reuse a compatible instance or create a project-specific one.
3. Ask separately before creating material under `~/.bro`, changing `~/.bro/creds.json`, or atomically changing `~/.bro.json`.
4. Wire the `openai` credential when section 3 selected it.
   Besides running the native loop, it backs free-form spell dispatch and other optional helpers in Claude Code sessions, which direct spell tools work without.
5. Confirm the expected GitHub user login or App bot login for the selected instance.
   For an App, also confirm its installation account and that the target repository belongs to the installation.

### Act

Keep `~/.bro` and `~/.bro/creds` at mode 0700 and material files at mode 0600.
Do every secret-producing step in a separate user terminal under `umask 077`.
Never read the result back into this conversation.
Compatible existing material is reused.
A partial or conflicting file is shown by metadata and resolved explicitly, never overwritten silently.

Claude Code always requires a scalar setup token.
Have the user run `claude setup-token` in the separate terminal and redirect its secret output directly to `~/.bro/creds/claude_code.cred`, or to a selected `claude_code+<instance>.cred`.
Verify only existence, nonzero size, mode, and later scope resolution.

For a selected OpenAI key, store a JSON object shaped as `{"api_key":"..."}` in `openai.cred` or the selected named material.
Use hidden terminal input and stdin-based JSON encoding so the value reaches neither command history nor a process argument.

For GitHub development:

- reuse a suitable stored GitHub instance when possible;
- otherwise, after `gh auth status`, redirect `gh auth token` directly into `github+<instance>.cred`;
- for a GitHub App, have the user create its JSON material outside chat and merge `"github+<instance>": {"type": "github_app"}` into `~/.bro/creds.json`;
- record only the expected non-secret acting identity.

When GitHub brog is chosen, create a project-specific material file with this shape:

```json
{
  "backend": "github",
  "token": {"$cred": "github"},
  "repo": "owner/name"
}
```

Name it `brog+<project-slug>.cred` unless a suitable compatible instance already exists.
The `$cred` reference follows this project's selected GitHub instance without duplicating a token.
The `repo` value is `owner/name`, not a Git URL.
Omit it only when `origin` is unambiguous and GitHub-shaped.

Show an exact non-secret merge into `~/.bro.json` before writing it atomically.
Preserve every unrelated top-level field, default, user layer, project, bro, and LLM preset.
A representative URL-keyed project layer is:

```json
{
  "projects": {
    "https://github.com/owner/name.git": {
      "creds": [
        "brog+<project-slug>",
        "github+<instance>"
      ]
    }
  }
}
```

Replace the representative key with the exact portable Git URL selected above;
do not change SSH to HTTPS, drop `.git`, or substitute a local-path origin.
When no portable URL identity exists, use the exact canonical checkout path.
Add named `claude_code` or `openai` instances only when selected.
Do not redundantly copy a choice already supplied correctly by `defaults`.
Do not put session credentials under `user`, because that branch affects host CLIs rather than managed launches.
Keep only machine-specific differences under the path identity.

No `anthropic` credential is needed.
No `trails` credential is needed because absence selects local recording.

After a newly selected brog resolves successfully, show and obtain approval for the pending repository edit that changes the developer's `features` value to `{'brog': True}`.
Leave it `False` if setup is declined or unsuccessful.
Rerun the registry probe, package build and inspection, one-SHA assertion, and both root image phases after this final declaration change.

From the target checkout and intended activated environment, run:

```console
command -v ride
command -v dive-in
ride scope --repo . --bro <developer-name>
```

Both executables must belong to the selected repository or sidecar environment, not a global tool, stale framework checkout, or another consumer.
If the developer became the default, also run `ride scope --repo .`.
When an OpenAI key was wired, also run the scope report with `--harness` naming the harness the project `harness` does not select, so both harnesses' scopes are checked.
Every required row must report `ok`, the intended instance, and the intended selection layer.
Only a declined `openai` row and the `trails` row may be missing.

**Checkpoint 5:** report material names and modes without values, the path and optional portable URL identities, host-config selection layers, and required plus optional scope rows.
Also report command provenance and the expected GitHub identity that the post-commit managed preflight must verify.

## 6. Review and commit the bootstrap

### Check

1. Review the complete diff and `git status --short`.
2. Confirm that dependency metadata, the lock, declaration module, project settings, ignore entries, and only approved repository instructions are present.
3. Confirm that no credential store, secret directory, unrelated dirty path, build output, virtual environment, or cache is staged.
4. Repeat formatting, focused tests, the registry probe, wheel inspection, lock convergence assertion, and both root image phases after the final edit.

### Ask

1. Show the exact paths proposed for staging and obtain repository-write approval.
2. Show the focused commit message and obtain commit approval.
3. If `origin` exists, ask separately whether to push or open the agreed setup pull request.

### Act

Stage only explicit bootstrap paths.
Create the focused commit.
A commit is mandatory because an attached boxed workspace clones `HEAD`; uncommitted checkout changes do not transfer even if an editable host install appears to work.
A fresh repository therefore needs an initial commit.
Leave unrelated modifications unstaged and uncommitted.

Carry out only the delivery approved above.
If a push was approved, push the focused commit on the agreed branch or ref.
If a setup pull request was approved, push its agreed branch when needed and open that pull request.
Record the pushed ref and pull-request URL, and do neither action when the user declined it.

Before any issue write, run a read-only managed-session preflight using the configured developer and the committed repository.
For either credential shape, query GraphQL `viewer { login }` through `gh api graphql` and compare the result with the expected user or App bot login.
For a user token, inspect `repos/owner/name` for repository role permissions.
For an App installation token, use paginated `gh api installation/repositories` output to require the exact target repository.
When GitHub brog was selected, also require that target repository's metadata to report `has_issues`.
Do not call `gh api installation`, which installation tokens cannot read, and do not interpret a repository object's user-role booleans as the App's granted permissions.
Treat user permission metadata or App installation access as preflight rather than proof of write authority.
The later brog issue creation is the first issue-write proof, while the eventual contents push and pull request prove those separate capabilities.
Stop on an identity mismatch, absent installation repository, incomplete metadata, or insufficient user permissions.
Stop on disabled Issues only when GitHub brog was selected.

If the setup commit is not reachable from `origin`'s default branch, record its SHA for the final `dive-in --into <sha>` command.
Do not imply that an unpushed local `HEAD` is the ordinary `dive-in` base, because `dive-in` prefers a freshly fetched origin default-branch tip.

**Checkpoint 6:** report the cleanly scoped commit SHA, whether it is reachable from the remote default branch, preserved unrelated work, and the exact `--into` consequence.
Also report the verified managed-session GitHub identity and repository access metadata.

## 7. Run the definition-of-done check

### Check

Confirm this is still a host session, Docker responds, the intended environment is active, required scope rows are `ok`, and the focused commit is `HEAD`.

### Ask

Show the exact command and obtain approval for the managed launch and its local Docker/session writes.
Do not add any flag that avoids a configured default.

### Act

From the checkout, run exactly:

```console
ride solo my-dev "sup?" --repo .
```

Substitute only the selected public name when it differs.
Do not add `--unboxed`, `--grant`, `--no-trails`, or `--llm`, and add `--harness` only on the second run below.
Success means exit status zero and a coherent reply from the harness the project `harness` selects.
The first run may build runtime and project images, so inspect active output before calling ordinary build latency a hang.

When an OpenAI key was wired, run the same command again with `--harness` naming the harness the project `harness` does not select, so both installed harnesses are proven;
without one, report the native harness as installed but unproven.

Diagnose failures from the earliest failing layer:

1. A wrong or missing command requires a sync, activation of the intended environment, and `type -a ride` inspection.
2. `unknown bro` requires checking entry-point metadata, class name, import path, package discovery, and the post-edit sync.
3. A `[tool.bro]` error requires fixing the closed schema, not inventing fallback keys.
4. A required `MISSING` scope row requires repairing material or host selection, not masking it with `--grant`.
5. A Claude auth error requires repairing the scalar `claude_code` setup token.
6. A native run refusing for a missing `bro` command requires `bro-native` in the synced environment and a rebuilt project image;
   an OpenAI auth error requires repairing the `openai` material.
7. A repository or ref error requires a valid committed `HEAD`.
8. A Docker error requires repairing the daemon.
   An unboxed pass is diagnostic evidence only.
9. A failed session is retained for inspection.
   Use `ride list` and the named workspace before considering `ride clean`, and never delete a dirty workspace reflexively.

Report the failed command, upstream cause, and correction before retrying.
Never declare a partial or host-only workaround complete.

**Checkpoint 7:** report each acceptance command run with its exit status and coherent-reply summary, the runtime/project image result, whether the other harness was proven or left unproven, and the retained workspace name if diagnosis was needed.

## 8. Reconcile or create the initial roster-design task

### Check

Present this task title and body verbatim to the user:

**Title:** `design the project's bro roster`

```markdown
## Goal

Discuss the repository's real users, recurring jobs, and operating scenarios with the user, then agree which specialized bros this project should implement.
Do not implement those bros in this task.

For every candidate bro, produce a conceptual draft covering:

- its name, one responsibility, triggering scenarios, and explicit non-goals;
- its skills and spells, including which behavior belongs in durable instructions rather than ad hoc chat;
- its system-prompt contribution and the project facts it must know;
- its toolsets, data sources, host commands, and any harness-native tools it narrows or blocks;
- its required and optional credentials, instance-isolation needs, and install hooks;
- its harness and LLM recipe, expected cost/latency trade-offs, and any bros it may summon;
- a concrete acceptance check.

Review overlap between candidates and keep one owner for each responsibility.
After the user accepts the roster, create one implementation task per accepted bro, plus separate shared-tool or credential tasks where those are independently reusable.
Record dependencies and an implementation order, and return links to all created tasks.

## Done

The user has approved the conceptual roster and each accepted bro has a ready, scoped implementation task.

<!-- bro-bootstrap-roster:v1 -->
```

### Ask

1. Ask the user to approve the task text.
2. If brog is configured, ask whether an earlier run returned a canonical URL for this task.
3. Explain that at most one issue may be created and that task creation is a separate outward write requiring approval.

### Act with brog

Before creating anything:

1. Validate a user-supplied prior ref through brog.
   Require the exact durable `bro-bootstrap-roster:v1` body marker and report canonical id, URL, and status.
2. Without a supplied ref, use the selected managed-session GitHub identity to run an exhaustive paginated `gh api` read of `repos/owner/name/issues?state=all&per_page=100` with `--paginate --slurp`.
3. Filter pull requests out, select bodies carrying the exact marker, and retain number, URL, state, and state reason without printing unrelated issue bodies.
4. Treat any API error, malformed page, uncertain pagination, or interrupted read as incomplete and stop without a write.
5. Validate every candidate through brog rather than trusting the GitHub listing alone.
6. Reuse exactly one valid match.
7. Stop for the user on multiple matches.
8. Permit creation only after a complete read proves zero matches and the user authorizes one issue write.
9. If the match is terminal (`done` or `dropped`), report that the handoff already completed.
   Do not recreate it or offer a `dive-in` command unless the user deliberately reopens it outside this bootstrap.

After the identity and repository preflight from checkpoint 6 succeeds, run the configured developer in a managed session with a quoted heredoc or equivalent safe multiline input.
Instruct it to perform the exhaustive read above, use brog to validate or create exactly one marked task, not start the task, and return canonical id, URL, and status.
Pass the full approved title and body.
Add the selected ordinary `--llm <preset>` when applicable.
Do not use `gh issue create`, because creation through brog and later brog prefetch are part of the acceptance contract.

If the managed reconciliation fails, diagnose brog and retry only after its cause is fixed.
Never infer absence from brog's capped list operation.

### Act without brog

Print the approved title and body for the user to file manually.
Do not run `dive-in --new`, because the trackerless `fix` workflow cannot create a task.
Prepare a bare `dive-in` prompt containing the same roster-design request plus one sentence requiring ready-to-file implementation task texts instead of tracker links.

**Checkpoint 8:** report brog state, exhaustive-read evidence, marker match count, canonical task id/URL/status when present, whether one authorized write occurred, and whether handoff remains open.

## 9. Hand off with dive-in

### Check

Confirm the setup commit base, selected environment, effective project default, optional preset, and task status.
Do not hand off a terminal task.

### Ask

Show the exact next command.
Starting the interactive roster session remains the user's choice; do not launch it silently.

### Act

With an open brog task, give:

```console
source .venv/bin/activate
dive-in --task <canonical-task-id-or-url>
```

Use `source bro_project/.venv/bin/activate` for a sidecar.
Add `--bro <developer-name>` when an intentional existing project default was preserved.
Add `--llm <project-slug>-developer` when the user chose a named Claude preset.
Add `--into <setup-commit-sha>` while the setup commit is not reachable from origin's default branch, and explain when that modifier becomes unnecessary.

Without brog, use the approved multiline roster prompt in bare mode:

```console
dive-in '<approved roster-design prompt>'
```

Use a quoted heredoc or equivalent safe multiline form in the actual command.
Apply the same `--bro`, `--llm`, and `--into` modifiers when required.

Finish with a concise inventory of repository files changed, active developer and harness configuration, framework SHA, and credential instance names plus selection layers without values.
Also report brog state, setup commit and reachability, successful acceptance command, roster task state, and the exact next command.
Ask the user to report the host result to the framework maintainers because real authentication, Docker launch, GitHub write authority, and attended `dive-in` behavior can be accepted only on their host.

**Checkpoint 9:** report the final inventory and exact next command, with no hidden prerequisite left unresolved.
