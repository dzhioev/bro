# Temporary design review for issue 487

This pull request carries a design document and draft plan, not implementation code.
It will be closed without merge after review.

## Design

### Outcome and boundaries

`BOOTSTRAP.md` is an executable prompt for an ordinary, attended Claude Code session running on the consumer's host.
It is not a user guide, a framework installer, or a scaffolding program.
The prompt makes the session inspect the target before changing it and settle user-owned choices interactively.
It then applies the smallest packaging shape that fits the repository, configures host-only credentials without exposing their values, and proves the result with the exact required launch.

The implementation adds only root `BOOTSTRAP.md` to this repository.
It adds no template file or command because the developer declaration and packaging have to be adapted to the consumer's existing project, while the framework's live source remains the authoritative template.
It does not expose `dzhioev/ppp` to the consumer.
PPP is design evidence only.
It registers `ppp-dev` through `[project.entry-points.bro]` and keeps `bro-dev` as a runtime dependency because the local class imports `Dev`.
It keeps `bro-ride` in the development group, tracks all framework distributions from the public Git repository through `[tool.uv.sources]`, and sets `[tool.bro] default`.

The prompt supports the full Claude Code harness only.
It neither installs `bro-native` nor recommends `--harness bro` or `--raw`.
The native harness can be designed later as one of the initial roster's follow-up tasks.

### Explicit assumptions

The coordinator accepted assumptions 1–10 on the user's behalf.
They are settled defaults rather than questions for implementation:

1. The consumer starts in an attended Claude Code session on a macOS or Ubuntu host, with access to the target checkout and permission to request writes under the user's home directory.
   `ride` deliberately refuses to start any managed launch from inside a container.
2. `uv` is the integration and lock manager.
   It is already a framework prerequisite, PPP uses it, and the framework documents no alternative consumer installation recipe.
3. Bare `ride` and `dive-in` come from the target repository's activated uv environment, exactly as they do in PPP.
   A repository-independent `uv tool` installation is rejected.
4. Consumer sessions use the full Claude Code harness only.
   The prompt neither installs `bro-native` nor recommends `--harness bro`, `--raw`, or Anthropic API-key auth.
   Because `[tool.bro] harness` is project-wide, an existing intentional `harness = "bro"` must be changed to `claude` with approval or the bootstrap is unsupported; the exact no-flag smoke has no per-bro escape hatch.
5. A fresh repository may become a minimal Python 3.12 distribution whose only shipped code is its bro declaration.
   A sidecar narrows the installation that `ride` freezes, but it does not suppress the automatic project-image bake triggered by an attached repository's root `uv.lock`.
   An existing uv repository is supported only when both project-image phases succeed under the runtime image's Linux, Python, network, and no-launcher-credential conditions.
   The first phase copies only the root lock plus root/workspace manifests and runs `uv sync --frozen --all-packages --all-groups --all-extras --no-install-workspace`.
   The second mounts the complete tracked build context and runs the same sync with workspace installation enabled.
   A non-workspace local path dependency can pass a full-tree sync but fail the manifest-only phase, so one host command is not sufficient evidence.
   Otherwise the prompt stops and names the missing framework bake-selection seam instead of claiming the sidecar solves it.
6. Framework source declarations track public `dzhioev/bro` `master`, while the committed `uv.lock` pins the revision actually installed.
   Bootstrap performs an approved upgrade/relock of `bro`, `bro-dev`, and `bro-ride`, asserts that all three resolve to one Git commit, and binds every source/manual/template read to that commit.
   It never combines current-`master` instructions with an older retained lock.
7. The no-flag Claude recipe is the framework's current Claude Code default.
   The closed `[tool.bro]` launch schema has no automatic per-project LLM-default key, so a non-default Claude recipe is a named `[tool.bro.llm]` preset passed explicitly at launch.
8. The developer class declares the current framework-project developer's bro-native OpenAI recipe, presently `gpt-5.6-sol` at high effort.
   Full Claude ignores this `llm_spec`, and it does not justify installing `bro-native`.
9. GitHub Issues is the recommended first brog because its backend is built in and a GitHub `origin` supplies the natural task home.
   A scalar token captured from an existing `gh` login is the default GitHub credential; an existing GitHub App instance remains supported through `creds.json`.
10. OpenAI is a recommended but optional auxiliary credential for free-form spell dispatch, and missing `trails` means local recording.
    No OpenAI credential is required for direct spell tools, and no `anthropic` credential is required for full Claude.

Design review adds assumptions 11–15 to settle previously unspecified consumer behavior:

11. The prompt is rerunnable and must converge from a partial bootstrap without duplicating dependencies, entry points, config, credentials, or task creation.
    A conflicting existing value is shown to the user and resolved explicitly rather than overwritten.
12. Repository edits happen only after the target is inside the current Claude Code session's writable project boundary.
    If the user chooses a target outside that boundary, the session gives a restart command from the target root and resumes the checklist there.
13. On first adoption the new developer bro becomes `[tool.bro] default`.
    If an already bro-enabled repository has an intentional different default, the prompt preserves it and adds `--bro <developer-name>` to default-dependent `ride scope` and `dive-in` commands.
14. `my-dev` in the goal's command is the default chosen-name token, not a mandatory alias.
    If the user chooses another public name, the acceptance command substitutes only that positional token because the registry requires an entry-point key to equal its class's `name` and cannot register the same class under a `my-dev` alias.
15. Persistent bootstrap credentials use the conventional `~/.bro` store with `BRO_STORE` unset.
    An inherited directed store is treated as another execution boundary and must be deliberately unset or handled outside this prompt before host wiring continues.

`BOOTSTRAP.md` states these assumptions and asks before taking a different path where the choice belongs to the user.

### Authoritative links in the prompt

The prompt names absolute GitHub paths so it still works when consumed from `raw.githubusercontent.com`.
At execution it resolves and locks one framework Git SHA, substitutes that SHA into every `github.com/.../blob/<sha>/...` or `raw.githubusercontent.com/.../<sha>/...` read, and reports it in the checkpoint.
It tells Claude Code to read the relevant parts before editing rather than reproducing them:

- `README.md` for prerequisites, distributions, extension entry points, and project configuration;
- root `AGENTS.md`, section **Adding a Bro**, plus `bro/registry.py`, for the declaration, entry-point, duplicate-name, and lazy-load contracts;
- `dev/bros/dev/__init__.py` for the generic `Dev` behavior being inherited;
- `local/bros/bro_dev/__init__.py` for the framework repository's specialized developer shape;
- `bro/workspace/project.py` for the closed scalar `[tool.bro]` launch schema and open sub-table rule;
- `bro/llm/llms/claude_code.py` and `bro/llm/llms/openai.py` for the current recipe names, defaults, and effort vocabulary;
- `bro/reference/ride.md`, especially **Per-project defaults** and **Scoped credential hydration**;
- `bro/reference/dive_in.md` and `ride/ride/dive_in.py` for task prefetch, default-bro resolution, origin-base selection, and launch forwarding;
- `bro/setup/AGENTS.md` and `bro/base/host_config.py` for store paths, material shapes, URL identity matching, selection precedence, and `$cred` references;
- `bro/brog/system.py` and `bro/brog/github.py` for the built-in GitHub config, accepted issue refs, acting identity, and repository derivation;
- `dev/bros/dev/spells/wire.md` for the user-facing credential-selection procedure.

The prompt does not copy the wire spell or either reference manual.
It includes only the concrete consumer values that those generic documents cannot supply.

### Prompt structure

Each numbered section in `BOOTSTRAP.md` has three explicit parts: **check**, **ask**, and **act**.
The session reports a short checkpoint after each section and stops on an unmet prerequisite instead of weakening the target.

#### 1. Establish the target and execution boundary

**Check:**

- Detect containers through `/.dockerenv`, `/run/.containerenv`, and Linux cgroup markers, then inspect the operating system.
- Resolve the current Git root, `git status --short --branch`, remotes, current branch, whether `HEAD` exists, and the current Claude Code writable project boundary.
- Read root and relevant subsystem instructions plus packaging files before running an existing setup script.
- Check `git`, Python 3.12+, `uv`, `claude`, and a responding Docker daemon with version or status commands.
- Check `BRO_STORE`; the conventional persistent-host setup requires it to be unset so `~/.bro` is the selected store.
- Check `gh --version` and `gh auth status` only when GitHub is being considered, without printing a token or a credential-bearing remote URL.

**Ask:**

- Use the current repository, another existing repository, or create a new one.
- Confirm the project name, one-sentence purpose, target path, and default branch for a new repository.
- Choose the developer bro's public name, defaulting to `my-dev`, and derive collision-free Python module and class names separately.
- Confirm whether the repository will live on GitHub, its owner and visibility when it must be created, whether GitHub Issues should be its brog, and whether setup may create or change `origin`.
- Explain that a local focused commit is required for the acceptance launch; ask separately whether the session may push it or open a setup PR.

**Act:**

- If the session is in a container, stop before repository or credential writes and give the user the host restart command.
- If the selected target is outside Claude Code's writable boundary, stop before edits and restart Claude Code from that target with the raw `BOOTSTRAP.md` URL.
- For a new repository, create the confirmed directory, run `git init -b <default-branch>`, add only confirmed starter files, and ensure the user has a Git identity before committing.
- Create a GitHub repository or change `origin` only after showing the exact outward action and receiving approval.
- For an existing repository, preserve unrelated dirty files and never stage them with bootstrap changes.
- Record completed checkpoints so a restarted or rerun prompt rechecks compatible state and continues instead of recreating it.

#### 2. Choose the packaging shape and add dependencies

**Check:**

- Determine whether the root is an installable PEP 621 project, a uv workspace, a non-Python repository, or a project owned by another packaging system.
- Determine which distribution and package-discovery rule would actually ship the bro module.
- Inspect the environment and lock for private, local-only, platform-specific, or otherwise unreproducible distributions, because `ride` freezes every installed distribution rather than only `bro`'s dependency closure.
- When the repository has a root `uv.lock`, inspect every uv workspace member, dependency group, extra, source override, and local path dependency for both Linux project-image phases.
- Model phase one with only `uv.lock`, the root `pyproject.toml`, and discovered workspace-member manifests present, plus `--no-install-workspace`.
  A non-workspace path source, private fetch, or platform-only dependency that needs omitted source is incompatible even when a full checkout sync passes.
- Model phase two with the complete tracked image build context and workspace installation enabled.
  `ride` performs both phases independently of which venv supplied the executable.
- Search installed and declared bro entry points plus existing `[tool.bro]` tables for conflicts before adding anything.
- Detect a partial prior bootstrap and compare its dependency roles, source pins, entry point, module, lock, and environment rather than appending a second copy.

**Ask:**

- In an existing uv-managed Python project, confirm that adding the declaration to its distribution or to an installed workspace member is acceptable.
- Use the integrated shape only when the project's packaging, complete activated environment, and both manifest-only plus full-tree root-lock bake phases can be reproduced by the Linux runtime materializer.
- Propose the isolated `bro_project/` sidecar when the repository uses another package manager, has ambiguous package discovery, or has no root `uv.lock` and the application environment itself is too broad.
- Explain that a sidecar cannot rescue an unsafe root uv lock because the attached repository independently triggers the full project bake.
  If that bake is not viable, stop before editing and identify a future `[tool.bro]` project-bake selection or suppression seam as prerequisite framework work.
- In an existing uv workspace whose complete bake is viable, choose deliberately between an installed member and an independent sidecar.
  The installed member shares the workspace environment.
  The independent sidecar stays excluded from member discovery and every uv command addresses it with `--project bro_project`.
  Do not let workspace globs absorb it accidentally.

**Act, integrated root or installed uv workspace member:**

Merge, rather than replace, these dependency roles into the distribution that owns the bro declaration and the operator group the repository's normal setup installs:

```toml
[project]
dependencies = [
  "bro[github,llm,runtime]",
  "bro-dev",
]

[dependency-groups]
dev = [
  "bro-ride",
]

[tool.uv.sources]
bro = { git = "https://github.com/dzhioev/bro", branch = "master" }
bro-dev = { git = "https://github.com/dzhioev/bro", subdirectory = "dev", branch = "master" }
bro-ride = { git = "https://github.com/dzhioev/bro", subdirectory = "ride", branch = "master" }
```

For a fresh root, also set a normalized distribution name, version `0.1.0`, a one-line description, and `requires-python = ">=3.12"` under `[project]`.
For an existing project, preserve its project metadata, dependency order, source conventions, group names, and established sync command.

After adding or reconciling the sources, show the pending upgrade and obtain approval before running the equivalent of:

```console
uv lock --upgrade-package bro --upgrade-package bro-dev --upgrade-package bro-ride
```

Use the selected project or workspace form of the command.
Read the resulting `uv.lock`, require `bro`, `bro-dev`, and `bro-ride` to carry the same `dzhioev/bro` commit, and use that SHA for all subsequent framework source reads and checkpoint output.
A retained older lock, mixed revisions, or a branch movement during setup causes a re-read and revalidation rather than silent continuation.

`bro-dev` is a runtime dependency because the consumer's class imports `bros.dev.Dev`.
`bro-ride` is an operator/development dependency that provides `ride` and `dive-in` to the host environment.
The `github` extra supports a GitHub App credential source, `llm` supports the optional OpenAI spell interpreter, and `runtime` supplies the MCP serving dependencies the declaration uses.

For a fresh integration-only root using `uv_build`, append this packaging declaration to the complete `[project]` and dependency tables above:

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

The prompt adjusts the key, module, class, and the existing backend's package-discovery configuration when the user chose another name.
It leaves `bros/` without an `__init__.py`, because it is a shared PEP 420 namespace, and puts the class in `bros/my_dev/__init__.py`.

**Act, sidecar:**

- Add `[tool.bro]` only to the repository-root `pyproject.toml`, creating a table-only root file when none exists.
  `ride --repo .` and `dive-in` read this root file even though their executable comes from the sidecar environment.
- Create `bro_project/pyproject.toml` as a complete Python 3.12 `uv_build` project with distribution name `<project-slug>-bros`, the same framework dependencies and Git sources, and `bro-ride` in its `dev` group.
- Register `my-dev = "<project_slug>_bros.my_dev:MyDev"`, set `module-root = "src"` and `module-name = ["<project_slug>_bros"]`, and ship `bro_project/src/<project_slug>_bros/__init__.py` plus `my_dev.py`.
- Derive `<project_slug>` as a valid, collision-free Python package name rather than interpolating the display name blindly.
- Run `uv sync --project bro_project --all-groups --all-extras`, activate with `source bro_project/.venv/bin/activate`, and build with `uv build --project bro_project`.
- Commit `bro_project/uv.lock`, while ignoring only `bro_project/.venv/`, build output, and normal Python cache files.

This fallback keeps an unrelated build backend and application lock untouched and gives bare `ride` an installation containing only the framework stack and consumer entry-point distribution.
It narrows the runtime bundle only; a root `uv.lock` still drives the attached repository's separate manifest-only and full-tree all-members, all-groups, all-extras project-image phases and must already pass both compatibility gates above.
Inside an existing uv workspace, keep this narrow shape outside the workspace's member patterns, adding an explicit workspace exclusion when needed, and invoke every uv operation with `--project bro_project`.
If the user instead chooses a real workspace member, follow the integrated-member path and use the workspace lock and environment.

If the repository already has a root `setup.sh`, preserve its flow and add only the sync and hook-install commands needed by the selected shape.
Do not force `--all-groups --all-extras` onto an existing application whose setup intentionally selects a narrower set; those flags are safe for the dedicated fresh and sidecar projects described here.
For a sidecar, use `uv run --project bro_project bro.dev.install` from the repository root.
If no setup entry point exists, do not add one by default because `ride` does not require it; record the explicit host setup and activation commands in the repository instructions.

#### 3. Write project launch settings

**Act:**

Merge this repository-root configuration:

```toml
[tool.bro]
default = "my-dev"
harness = "claude"
image-repository = "bro/<project-unique-docker-slug>"
```

Before merging, inspect any existing project-wide `harness` value.
An intentional `bro` value presents a forced choice: approve changing every default launch to `claude`, or stop because the required no-flag smoke cannot select full Claude per bro.
`default` is what `dive-in` and attached `ride scope` use when `--bro` is omitted.
Mode verbs still take the bro positional, which is why the acceptance command names it.
Every `my-dev` value in these snippets is replaced consistently when the user chooses another public name.
On first adoption the prompt selects `my-dev`; if an intentional existing default is preserved, every default-dependent command in the prompt carries `--bro <developer-name>`.
The image repository slug is lowercase, Docker-compatible, and unique among this host's consumer repositories so common bro names do not share `bro/my-dev`.
The prompt leaves `summon-depth`, `build-context-command`, and other sub-tables absent unless a discovered project requirement needs them.

The session asks whether the framework's current Claude model and effort defaults are acceptable.
If not, it writes a project-specific named preset rather than inventing an unsupported scalar key:

```toml
[tool.bro.llm]
<project-slug>-developer = "claude-code:<model>:<effort>"
```

Host `~/.bro.json` LLM presets override project presets by name, so the prompt inspects that table and chooses a non-conflicting project-specific key.
It validates provider, model alias, and effort against the linked Claude recipe source and adds `--llm <project-slug>-developer` to ordinary `ride` and `dive-in` commands.
The exact no-flag definition-of-done command intentionally continues to test the framework default because `[tool.bro]` has no automatic LLM-default field.

#### 4. Define the consumer developer bro

The prompt first asks for the project's durable facts: repository purpose, where conventions live, formatter, focused and full test commands, build/package checks, generated-file rules, deployment boundary, and any paths that must never be staged.
It creates or updates repository instructions only with facts the user confirms.
It does not paste a large project map into the bro prompt.

The generated class follows this shape, with project-specific text and conditional lines filled from the earlier choices:

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
    model='gpt-5.6-sol', reasoning_effort='high'
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

The prompt reads the framework developer source at execution time and uses its current OpenAI model if the literal above has moved.
The user may choose another valid native recipe, even though full Claude sessions ignore `llm_spec`.

The variable lines are deterministic:

- Set `features = {'brog': True}` only after the user chooses and successfully configures brog.
- Set `features = {'brog': False}` when the user declines brog, so an unrelated ambient `brog` credential cannot silently enable this repository's task workflow.
- Include `extra_secrets = ('github',)` when the developer is expected to push, open PRs, or use the GitHub workflow.
- Omit that extra secret for a genuinely local or non-GitHub workflow, so the acceptance launch does not require an irrelevant credential.
- Do not redeclare Dev's file, shell, search, style-source, spell, or hook-provisioning components.
  They arrive through inheritance.
- Do not declare `may_summon` until the initial roster design has identified real collaborators.

After writing the module and entry point, sync again because entry points are installation metadata:

```console
uv sync --all-groups --all-extras
uv run python - <<'PY'
from bro.registry import create_bro, declared_specs

assert declared_specs()['my-dev'] == 'bros.my_dev:MyDev'
assert create_bro('my-dev').name == 'my-dev'
PY
uv run bro.dev.install
uv build
```

For an existing integrated application, use its established sync and build commands while ensuring the group containing `bro-ride` is installed.
For a sidecar, use `uv sync --project bro_project --all-groups --all-extras`, `uv run --project bro_project python ...`, `uv run --project bro_project bro.dev.install`, and `uv build --project bro_project`.
Adjust the asserted target for the chosen package shape and name.
Inspect the built wheel for the bro module and the `bro` entry point so an editable-only import cannot hide missing wheel contents.

#### 5. Prepare host credentials and wire their instances

The prompt reads the wire spell and host-config schema before changing host state, and it verifies that `BRO_STORE` is unset for this persistent setup.
A directed `BRO_STORE` is already a synthesized or service store and is not silently mixed with `~/.bro`.
The prompt never asks the user to paste a token into the Claude conversation, never prints credential contents, never embeds a secret in shell history or process arguments, and never writes credentials into the repository.
Secret capture happens in a separate user terminal through hidden input or direct stdout redirection under `umask 077`, followed only by metadata and availability checks.
Compatible existing material is reused; a conflicting source annotation or partial file is not overwritten without approval.
Keep `~/.bro` and `~/.bro/creds` mode 0700, every material file mode 0600, and host-config replacement atomic while preserving unrelated JSON fields.

**Always required for full Claude:**

- Run `claude setup-token` in the user's separate terminal.
- Store the returned scalar token at `~/.bro/creds/claude_code.cred`, or at a named `claude_code+<instance>.cred` selected for this project.
- Verify only that the file is non-empty, has the right mode, and resolves in `ride scope`; never read it back into the conversation.

**Recommended auxiliary OpenAI:**

- Ask whether the user wants free-form spell dispatch and other optional OpenAI-backed helpers.
- Store `{"api_key":"..."}` at `~/.bro/creds/openai.cred`, or a named instance selected for this project.
- Explain that its absence is allowed: direct spell tools remain, while `bro::cast` and other OpenAI-backed optional paths are unavailable.

**GitHub development credential when selected:**

- Reuse a suitable stored `github+<instance>` when one exists.
- Otherwise, after `gh auth status` succeeds, capture `gh auth token` directly into `~/.bro/creds/github+<instance>.cred` without sending it through chat.
- If the user chooses a GitHub App instead, store its JSON config in that material path and merge `"github+<instance>": {"type": "github_app"}` into `~/.bro/creds.json`.
- Record the expected non-secret identity when the user chooses the instance.
- After scoped launch is available, probe that selected instance inside the managed session.
  Use `gh api user` for a user token or `gh api installation` for an App token.
  Then inspect `repos/owner/name` for `has_issues` and repository permission metadata.
- Compare the reported user login or App slug and installation account with the expected identity before any write.
- Treat permission metadata as preflight rather than proof: the brog issue creation proves issue-write authority, while the first contents push and pull request remain the eventual write tests for those capabilities.

**Strongly recommended GitHub brog:**

- Require a GitHub repository with Issues enabled and an acting GitHub credential that can create and update issues and comments.
- Store a project-specific config such as this at `~/.bro/creds/brog+<project-slug>.cred`:

```json
{
  "backend": "github",
  "token": {"$cred": "github"},
  "repo": "owner/name"
}
```

The `$cred` node makes brog follow this project's selected GitHub instance instead of duplicating a token.
An explicit repository catches a wrong remote; omitting it is valid only when `origin` is unambiguous and GitHub-shaped.

**Selection:**

- Run `credentials list` and `credentials list --instance` to inspect kinds and stored names without resolving or printing values.
- Prefer the exact resolved output of `git remote get-url origin` as the project identity because URL matching lowercases only the scheme and hostname and trims trailing slashes; it preserves path case, `.git`, userinfo, and transport spelling.
- If that remote embeds credentials, replace it with an approved credential-free SSH or HTTPS URL before recording the identity; never copy userinfo into `~/.bro.json` or a report.
- Use the canonical checkout path only before a remote exists or for a machine-specific override; once a remote is created, move portable choices to its exact URL key.
- Show the proposed `~/.bro.json` merge before writing it, preserve every unrelated section, and write only named-instance selections the project needs.

For the common HTTPS remote ending in `.git`, a representative merge is:

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

The brog config's `repo` remains `owner/name`; it is not a Git URL.
Named `claude_code` or `openai` instances join that list when used.
Empty-instance material needs no explicit selection unless another layer would otherwise choose a different instance.
Selections already supplied correctly by `defaults` are preserved and explicitly acknowledged rather than redundantly copied into the project layer.
The prompt does not put session credentials under `user`, because that branch applies to host CLIs and never to managed launches.

No `trails` credential is required.
A missing optional `trails` entry selects local recording under the global ride runtime root.
No `anthropic` credential is required because raw mode is out of scope.

#### 6. Validate the declaration, scope, and committed base

Run these checks from the target checkout with the selected uv environment active:

```console
command -v ride
command -v dive-in
ride scope --repo . --bro my-dev
```

Both commands must resolve inside the selected repository or sidecar venv, not to a global tool, stale framework checkout, or another consumer's installation.
If an intentional existing project default was preserved, keep `--bro <developer-name>`; otherwise also run attached `ride scope --repo .` to prove default resolution.
Every required row in `ride scope` must report `ok`.
Every instance and printed selection layer must match the reviewed intent, whether it comes from the project URL, project path, project-bro, defaults, or the empty instance.
`openai` and `trails` may report `MISSING` only when the user accepted those optional fallbacks.

Before the launch, review the diff and make a focused commit containing the dependency metadata, lock, bro module, project settings, and any confirmed repository instructions.
This is required even though a dirty editable installation can make the smoke appear to work: a path attachment clones `HEAD`, and uncommitted checkout changes do not transfer into the managed workspace.
A fresh repository therefore needs an initial commit.
An existing repository's unrelated dirty changes remain uncommitted and unstaged.

If `origin` is present, either push or merge the setup commit with the user's authorization, or record its SHA for the first `dive-in --into <sha>` command.
This matters because `dive-in` normally fetches and bases on `origin`'s default-branch tip rather than an unpushed local `HEAD`.

#### 7. Run the definition-of-done check

From the checkout and with the venv active, run this exact shape:

```console
ride solo my-dev "sup?" --repo .
```

`my-dev` is literal when the user accepted the suggested name; otherwise substitute only the selected public bro name.
Do not register an alias merely to preserve the example token because the registry requires every entry-point key to equal its class's `name`.
Success means exit status zero and a coherent Claude reply.
The first run may build the runtime and project images, so ordinary build latency is not treated as a hang without checking process output.
The prompt does not add `--host`, `--grant`, `--no-trails`, `--harness`, or `--llm` to this command because each would avoid part of the configuration being accepted.

On failure, diagnose from the earliest failing layer and rerun only after fixing its cause:

1. A wrong or missing command means rerun `uv sync`, reactivate the intended venv, and inspect `type -a ride`.
2. `unknown bro` means verify the entry-point key, class `name`, import path, package discovery, and the post-edit sync.
3. A `[tool.bro]` error means validate the root table against the documented closed schema; do not add permissive fallback keys.
4. A `MISSING` required scope row means repair the material or `~/.bro.json` selection through the wire procedure; do not mask it with `--grant` in the acceptance command.
5. A Claude auth error means repair the scalar `claude_code` setup token.
6. A repository or ref error means ensure the checkout has a valid committed `HEAD`.
7. A Docker error means start or repair the daemon; a host-mode pass is diagnostic evidence only and does not satisfy the definition of done.
8. The in-container refusal means continue from a host Claude Code session.
9. A failed session is retained for inspection, so use `ride list` and its named workspace before considering `ride clean`; never delete a dirty workspace as a reflex.

The session reports the failing command, root cause, and correction.
It never declares a partial or host-only workaround done.

#### 8. Create the initial roster-design task

After the smoke passes, present this task to the user for approval.
The title is `design the project's bro roster`.
The body is:

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

When brog is configured, the bootstrap session first asks whether a prior run already returned this task's canonical URL.
If supplied, the managed developer validates that ref through brog, including terminal status and the durable `bro-bootstrap-roster:v1` body marker.
Without a ref, the selected GitHub identity performs a read-only, paginated all-status issue enumeration through `gh api`, filters out pull requests, and validates candidates through brog.
Exactly one matching marker is reused.
Zero matches permits creation only after enumeration completed without truncation; ambiguous or incomplete enumeration stops without a write and returns the evidence for the attended bootstrap session to resolve.
A terminal match means the roster handoff already completed: report its URL and status, do not recreate it, and do not offer a `dive-in` command for that closed task unless the user deliberately reopens it outside bootstrap.

After the selected GitHub identity and repository permission preflight passes, the bootstrap session asks the working developer bro to reconcile or create the task and return its canonical id, URL, and status:

```console
ride solo <developer-name> '<reconcile the marked task or create exactly one through brog; do not start it; return its id, URL, and status>' --repo .
```

The actual command uses a heredoc or equivalent safe quoting for the full title and body and explicitly authorizes at most one issue creation.
It carries `--llm <project-slug>-developer` when a named Claude preset was selected; unlike the acceptance smoke, this is an ordinary launch.
If this fails, diagnose brog and rerun; do not bypass the backend with `gh issue create`, because creation through the selected brog and later task-prefetch are part of the handoff contract.

When brog is not configured, print the title and body verbatim for the user and do not call `dive-in --new`.
The trackerless rendering of Dev's `fix` spell deliberately stops, so `--new` cannot create the task.
Instead, use bare `dive-in` with the same body as its initial conversational prompt and add one sentence telling the session to return ready-to-file implementation task texts rather than tracker links.

#### 9. Hand off with `dive-in`

With brog, finish with:

```console
source .venv/bin/activate
dive-in --task <canonical-task-id-or-url>
```

Use `source bro_project/.venv/bin/activate` for the sidecar.
Add `--bro <developer-name>` when an intentional existing project default was preserved.
Add `--llm <project-slug>-developer` when the user chose a named Claude preset.
Add `--into <setup-commit-sha>` when the setup is not yet reachable from `origin`'s default branch, and explain that the ordinary command becomes sufficient after merge or push.

Without brog, finish with the equivalent bare form:

```console
dive-in '<the approved roster-design prompt>'
```

Use a quoted heredoc or equivalent for this multi-line prompt, and apply the same `--bro`, `--llm`, and `--into` modifiers when they are needed.
The prompt closes with a concise inventory of repository files changed, the active bro and harness, credential instance names and selection layers without values, brog state, the setup commit, the successful smoke, and the exact next command.

### Why resolution works

The bare `ride` executable comes from the activated uv environment.
That same installation contains `bro-ride`, `bro-dev`, core `bro`, and the installed consumer distribution.
`ride` freezes every distribution in the invoking installation into its runtime bundle, while `bro.registry` discovers `my-dev` from the consumer distribution's `bro` entry point.
That whole-environment freeze is why an application with private or platform-specific dependencies uses the narrow sidecar environment even when its build backend could technically ship the module.
The repository contributes no bro through `[tool.bro]`; that table only names an already-installed entry point as its project default and selects the Claude harness.

`ride solo my-dev ... --repo .` binds the checkout explicitly, reads the root project config, and resolves the named bro before creating a workspace.
It computes the full-Claude scope as the bro's Claude-surface manifest plus required `claude_code`, then hydrates only the selected credential kinds.
The class's native OpenAI `llm_spec` is excluded on this surface.
OpenAI enters only as an optional spell-casting credential, while `features = {'brog': True}` makes `brog` required when selected.

`dive-in` is different only in being cwd-bound.
It resolves the checkout and `[tool.bro] default`, then computes the same prospective harness and credential scope.
Before workspace launch it prefetches the task through that selected brog, sets `RIDE_TASK_ID`, and seeds `[[fix <ref>]]` into a fresh `ride along` workspace.

### Stress walk: fresh repository

1. Claude starts on the host in an empty target directory, confirms macOS or Ubuntu, the toolchain, Docker, project name, `my-dev`, GitHub visibility, brog, model choices, and commit permission.
2. It initializes Git, writes the minimal uv package, the `bros.my_dev` class, unique image repository, lock, ignore rules, and only user-confirmed project instructions.
3. `uv sync`, the registry probe, the package build, and `bro.dev.install` pass.
4. The user authenticates GitHub if selected, the session creates or confirms `origin`, and the focused setup commit is pushed when authorized.
5. The user supplies the Claude setup token outside chat.
   Optional OpenAI and selected GitHub material are stored with restrictive modes.
   The session creates the `$cred`-based brog config and atomically merges project selections under the exact credential-free `origin` URL.
6. `ride scope` shows `claude_code`, `github`, and `brog` as required and correctly selected, with OpenAI optional and trails local.
7. The exact solo check runs against committed `HEAD` in a real container and replies successfully.
8. A second authorized solo run verifies the selected GitHub identity and repository permissions, reconciles the durable roster marker, creates the issue through brog only when absent, and returns its URL and status.
9. The session gives `dive-in --task <url>`, with the selected preset if any.

The unborn-branch failure, missing remote, first commit, image build, credential scope, initial issue write, and final prefetch all have an explicit place in the flow.

### Stress walk: existing repository

1. Claude reads the existing instructions, status, remotes, packaging, lock, setup path, and package discovery before proposing a diff.
2. It leaves an unrelated modified file untouched and creates a setup branch if that repository's policy requires one.
3. It classifies compatible, partial, and conflicting prior bro setup before editing, and a rerun never creates a second dependency, entry point, credential instance, or initial task.
4. For a root uv lock, it separately validates the manifest-only `--no-install-workspace` layer and the complete tracked-tree installation layer under the runtime image conditions.
   A non-workspace path source is tested as an omitted-source risk rather than inferred safe from a successful checkout sync.
5. In a reproducible uv-managed PEP 621 project it merges dependencies, sources, the entry point, and `[tool.bro]` into the owning distribution or an installed workspace member and updates package discovery deliberately.
   In a Poetry, Node-only, or table-only repository without a conflicting root uv bake, it can use `bro_project/` and leave the application backend and lock alone.
6. It reruns the established sync after entry-point metadata changes and builds the affected distribution, catching a module that worked only from the source tree.
7. The wire pass inventories existing credential instances, reuses suitable ones, shows the exact host-config merge keyed by the actual origin spelling, and verifies every selected layer with `ride scope` without printing values.
8. It stages only bootstrap paths and commits them.
   The exact solo check uses local `HEAD`, while the final dive-in command carries `--into <sha>` until the setup commit reaches the remote default branch.
9. Brog creation and task prefetch use the configured repository, not whichever GitHub identity or task backend is globally default.
   Before creating it, the session accepts and verifies a user-supplied prior URL or exhaustively enumerates all-status GitHub issues for the durable body marker.
   It creates only after proving zero matches, stops on ambiguity or truncation, and treats a terminal match as an already completed handoff.
10. If the repository's existing project default remains intentional, final handoff names `--bro <developer-name>`; otherwise omission verifies the new default.

This walk leaves existing application dependencies, dirty work, package-manager policy, host defaults, existing bro defaults, and task history intact.

### Verification during implementation

A later implementation session can verify the prompt from this managed container without changing the framework runtime:

- run the repository Markdown and full test gates, including link and packaging policies that already cover root documentation;
- fetch every absolute source link and check that its named heading or file exists;
- create a throwaway fresh Git repository from the prompt's minimal `uv_build` shape, sync it against the public Git sources, build it, and prove `create_bro('my-dev')` resolves through installed entry-point metadata;
- create a throwaway existing non-Python repository with unrelated dirty work, add the sidecar shape, and prove sync, build, registry resolution, and status preservation;
- create a root uv-lock fixture with a non-workspace local path dependency whose full-tree sync passes but whose manifest-only `--no-install-workspace` phase fails;
  prove that a sidecar still selects this project bake, exercise both Dockerfile phases or an equivalent narrow image check, and verify the prompt rejects the incompatible repository rather than claiming isolation;
- seed a retained framework lock, run the documented approved relock, assert `bro`, `bro-dev`, and `bro-ride` resolve to one SHA, and verify every authoritative URL uses it;
- unset inherited `BRO_STORE` and point `HOME` and `XDG_DATA_HOME` at throwaway directories;
  create fake non-production credential material and an origin-keyed host selection including the real `.git` suffix;
  verify `ride scope --repo . --bro my-dev` reports the expected required, optional, and instance layers;
- run trackerless `dive-in --dry-run '<prompt>'` and inspect the composed `ride along --repo ... my-dev ...` command;
- run the exact solo command only to confirm the documented in-container refusal, not as acceptance evidence.

Only the user's post-landing host run can cover the real definition of done:

- actual host PATH and venv activation;
- Claude setup-token authentication;
- the user's existing credential store and `~/.bro.json` merge;
- the Docker daemon, runtime/project image build, container start, and Claude reply;
- real GitHub token or App permissions, Issues availability, brog task creation, and comment authorship;
- `dive-in` task prefetch and the interactive attended session;
- the user's answers about project policy, model/cost preferences, GitHub visibility, and commit delivery.

There is no separate framework verification phase after implementation.
The prompt itself tells the user to report the host outcome, in line with the task's settled rollout.

### Edge cases and risks

- A common bro name makes Docker's default image repository common too, so the prompt sets a project-specific `image-repository`.
- A globally installed or another checkout's `ride` may not know the consumer entry point, so both command paths are checked after activating the selected venv.
- An inherited `BRO_STORE` redirects credential reads away from `~/.bro`, so persistent host setup refuses to continue until that environment override is understood and unset.
- Host project identity matching preserves `.git` and transport spelling; copying a simplified GitHub URL into `~/.bro.json` can leave every intended selection unapplied.
- A host LLM preset with the same name overrides the project preset, so the generated key is project-specific and collision-checked.
- Entry points are metadata, so editing `[project.entry-points.bro]` without syncing leaves `unknown bro` even when the module imports by path.
- A dirty editable distribution is frozen with its dirty contents and can create a false-positive smoke, while the attached workspace receives committed `HEAD`; the focused commit closes that gap.
- An existing reachable `origin` makes `dive-in` prefer remote default-branch `HEAD`; an unpushed setup commit therefore needs `--into`.
- A project-specific brog instance can reference the selected GitHub kind safely, but a literal token in brog duplicates secret state and is rejected by the prompt.
- A token may authenticate successfully while lacking Issues, contents, or pull-request permissions; scope proves presence, while task creation and later workflow actions prove authority.
- GitHub Issues may be disabled, `origin` may not be GitHub-shaped, or an organization may require SSO approval.
  The prompt fixes or explicitly declines brog before choosing the class feature value.
- Optional OpenAI can incur separate cost even though Claude is the primary harness.
  The user opts in with that distinction stated.
- The framework tracks a moving branch and has no compatibility promise.
  An approved relock pins all three installed distributions to one SHA, and every authoritative read uses that SHA so current docs cannot silently configure older code.
- `[tool.bro] harness` is global rather than per bro.
  An existing native default must move to Claude with approval or the exact no-flag acceptance shape is unsupported.
- Brog's list surface has a hard 100-item cap and no pagination.
  Idempotent task reconciliation therefore uses the selected GitHub API read path, all statuses, a durable body marker, and fail-safe no-write behavior.
- Freezing a large existing application environment can include private or platform-specific distributions.
  A sidecar narrows that runtime bundle, but it cannot suppress an attached root uv lock's separate project-image bake.
- The project bake has a manifest-only dependency layer before its full tracked-tree installation layer.
  Both install every root workspace member, group, and extra under Linux, and the first uses `--no-install-workspace` with no non-workspace source tree present.
  A root uv lock that cannot complete either phase makes the repository unsupported until a framework project-bake selection or suppression seam exists.
- A private VCS dependency may be reachable on the host but not from either container materializer, which intentionally has no launcher credentials.
- Windows is not claimed as supported by this bootstrap because the repository documents only macOS and Ubuntu setup and the runtime uses Unix host primitives.
- A Claude Code session opened against another project may not have a safe writable boundary for the chosen target; the prompt restarts from the target root before editing.
- A partial or repeated bootstrap can otherwise duplicate an entry point, overwrite an intentional default, or create the roster task twice; every section begins with state reconciliation.
- Home-directory writes may be blocked by the current Claude permission mode.
  The prompt requests approval or gives exact user-run commands instead of moving secrets into the checkout.
- Multiple existing credential instances make silent selection dangerous.
  The wire procedure presents non-secret metadata and requires the user to choose.
- The initial task creation is an outward write.
  The prompt shows the exact task first and authorizes a single managed solo run only after the user accepts it.

### Rejected alternatives

- **A new scaffolding command:** out of scope and premature before the human-driven flow has been exercised on more than one consumer.
- **Copying Dev, its spells, or the manuals into the consumer:** inheritance and links keep one source of truth and preserve future framework fixes.
- **Showing PPP to the user:** the task explicitly makes it design evidence rather than part of the consumer prompt.
- **A global `uv tool install bro-ride`:** it does not contain the consumer's entry-point distribution by default.
  Installing one consumer editably into a single global tool environment also creates replacement and executable-collision problems for additional repositories.
  A project-local venv makes command provenance explicit and matches the example consumer.
- **Installing `bro-native`:** unnecessary for full Claude and contrary to the settled consumer-harness scope.
- **Using `--raw` with an Anthropic API key:** it is a different Claude flavor and bypasses the supported full-harness authentication path.
- **Inventing `[tool.bro]` keys for model, auxiliary LLM, or credentials:** the project schema is closed; models use a named LLM preset or class `llm_spec`, while secret material and instance choice live in the host store and host config.
- **Treating a named LLM preset as automatic:** current code expands it only when `--llm <name>` is passed.
- **Always requiring brog or GitHub:** that makes the exact smoke impossible for an intentionally local setup.
  The class pins brog on or off from the explicit choice and requires GitHub only for the selected workflow.
- **Leaving the inherited brog feature implicit:** an unrelated ambient empty-instance config could turn it on, while a project-only named instance may not turn it on during declaration assembly.
  The consumer class states the user's decision.
- **Putting tokens in repository config, environment files, or chat:** credential stores and `$cred` references provide the intended isolation and rotation boundary.
- **Using one-time `--grant` flags as setup:** they bypass persistent project selection and would make the acceptance command prove less than the definition of done.
- **Creating the initial GitHub issue with `gh issue create`:** it would not prove that the configured brog and dive-in prefetch path work.
- **Using `dive-in --new` without brog:** the trackerless `fix` spell deliberately cannot create a task; bare dive-in is the correct text-only fallback.
- **Treating the sidecar as a project-bake switch:** it changes the invoking installation, not either attached root-lock image layer; unsupported locks fail explicitly instead of relying on file renames or uncommitted manifest edits.
- **Testing only a full-tree uv sync:** the real image first resolves from root/workspace manifests with `--no-install-workspace`, so both phases belong to the support gate.
- **Reading templates from current `master` after retaining an older lock:** every authoritative read is bound to the one SHA the three installed distributions share.
- **Using capped open-task listing as proof of absence:** initial-task reconciliation enumerates all GitHub issue statuses and refuses a write when the read is incomplete or ambiguous.
- **Accepting `ride ... --host` instead of the exact check:** it avoids the required default container path and cannot validate Docker setup.
- **Adding a separate checked-in template file:** the prompt already has to synthesize project-specific packaging and text, and the live framework declarations are better templates than a second frozen copy.


## Design changelog

No implementation-time design deviations have been recorded yet.
Implementation stages append dated decisions here rather than rewriting design history.

## Draft implementation plan

Integration target: `integration/487-bootstrap-prompt`.

### Stage 1 — write and verify the consumer bootstrap prompt

**Goal:**

Add root `BOOTSTRAP.md` as the attended Claude Code prompt described by the design, and prove its repository-facing instructions against the current framework without performing the user's real host rollout.

**Details:**

1. Write the prompt as a check/ask/act workflow with explicit checkpoints, restart boundaries, idempotent partial-setup handling, and user confirmation before repository, home-directory, or outward writes.
2. Cover fresh integrated packages, safe integration into an existing uv project or workspace member, and the independent `bro_project/` sidecar where it actually narrows the invoking installation.
   Gate every root uv lock on both real Linux image phases: manifest-only resolution with `--no-install-workspace`, then full tracked-tree workspace installation.
   Stop unsupported repositories instead of presenting the sidecar or a successful full-tree host sync as proof that the bake works.
3. Carry the verified registry, project config, credential, GitHub brog, committed-base, roster-task, and `dive-in` contracts into the prompt without exposing PPP.
   Cover the full-Claude project-wide harness, opt-in LLM preset, exact-origin identity, and exact smoke-command shape explicitly.
   Relock `bro`, `bro-dev`, and `bro-ride` to one reviewed SHA and bind all source reads to it.
4. Keep secrets out of chat, output, shell history, process arguments, and the repository; make the prompt use metadata-only checks and the wire procedure.
   Probe the selected managed-session GitHub identity and repository permissions before the initial issue write.
5. Make roster-task handoff rerunnable through a durable body marker, exhaustive all-status GitHub issue enumeration, brog validation and creation, terminal-task handling, and fail-safe no-write behavior on ambiguity or incomplete reads.
6. Run the repository formatter and full test gate, and check every SHA-bound source link.
   Build and inspect fresh plus sidecar fixture wheels, prove registry resolution, exercise fake scoped credentials with inherited `BRO_STORE` removed, and inspect trackerless `dive-in --dry-run`.
   Verify retained-lock convergence and prove that a sidecar does not suppress either root-lock image phase.
   Include a non-workspace path-source fixture whose full-tree sync passes while manifest-only resolution fails.
   Confirm that the exact solo shape takes the documented in-container refusal in the implementation environment.
7. Land the stage through `[[run pr]]` with base `integration/487-bootstrap-prompt`.

A single stage is intentional.
The deliverable is one tightly coupled prompt file, and separating its prose from the fixtures that validate the same commands would create a stage boundary with no independently useful result.

### Prerequisites and rollout boundary

No framework code or scaffolding command is a prerequisite for repositories whose project-image bake passes the documented compatibility gate.
A repository with an incompatible root uv lock remains unsupported until separate framework work adds a project-bake selection or suppression seam; this stage must report that boundary rather than implement the seam.
Implementation needs network access to the public framework Git sources and the repository's normal uv test environment.
Real Claude setup-token auth, host Docker launch, the user's credential store, GitHub issue creation, and interactive `dive-in` remain the one post-integration user rollout described by the root task rather than another implementation stage.
