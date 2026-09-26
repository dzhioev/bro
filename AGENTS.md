# Bro framework

Bro is the agent system:
independent specialised agents (a "Bro") each run as a stateless LLM loop with their own system prompt and MCP-tool access.
The conceptual model is `README.md`;
this file maps the repository and carries the rules every change follows.
Every subsystem carries its own `AGENTS.md`, titled after what the subsystem is rather than the path it sits at:
read the one for each subsystem you touch, and follow its pointers as questions arise rather than reading ahead.
Run any script with `--help` for flags.

## Map

The repository is a uv workspace.
All published members depend on `bro`;
core imports none of them, and `bro-ride` spawns rather than imports `bro-native`.

| Directory | Distribution | What it is | Map |
|---|---|---|---|
| `bro/`, `bros/bro/` | `bro` | the framework core: the persona declaration and what composes and runs one, the declaration vocabulary, and the minimal `bro` persona with the spells every bro inherits | `bro/AGENTS.md` |
| `native/` | `bro-native` | the native engine and `bro` command | `native/AGENTS.md` |
| `dev/` | `bro-dev` | the `bro.dev` and `bro.workflow` packages, `poll-pr` and `pr-state`, and the development personas | `dev/AGENTS.md` |
| `ride/` | `bro-ride` | top-level `ride`, the managed-workspace runtime and both harness adapters | `ride/AGENTS.md` |
| `oops/` | `bro-oops` | consumer-neutral deployment and operations machinery | `oops/AGENTS.md` |
| `bench/` | `bro-bench` | the launcher-side benchmark credentials, registered worker type, and session commands | `bench/AGENTS.md` |
| `webview/` | `bro-webview` | the registered browser worker type, owner command, image, and daemon | `webview/AGENTS.md` |
| `local/` | `bro-local` | this checkout's own personas and policy scripts, kept out of every published wheel by riding the root's `dev` dependency group | `local/AGENTS.md` |
| `benchmark/` | `bro-benchmark` | the Terminal-Bench harness adapter; deliberately **not** a member, it locks, syncs and tests in an environment of its own | `benchmark/AGENTS.md` |

The root also carries `pyproject.toml` (core distribution metadata, the workspace table, and the tool config every member runs under),
`conftest.py` (test isolation: `local/AGENTS.md`, "Test gate"),
and `README.md` (the front page: the framework's features and limits, shown on one example crew, linking into the references).
Reference docs for framework users live in `bro/reference/` and ship in the wheel:
`extending.md` (declaring and registering a bro, adding a data source or a toolset, the entry-point groups), `conditions.md` and `template.md` (conditioning in code and in text), `ride.md` (the runtime), and `dive_in.md`.
`BOOTSTRAP.md` is the executable checklist for adopting the framework in another repository.

## Development

`./setup.sh` syncs the workspace and installs the repository hooks;
it leaves `benchmark/.venv` alone, which is synced on demand, by the gate stage or by hand.
Run the repository's console scripts and its own shell scripts through `uv run -q <command>` (`uv run -q ./format.sh`, `uv run -q run-tests --changed`) or `.venv/bin/<command>`;
`-q` keeps uv's own sync report out of the command's output.
A session that carries a `bro::banner` tool runs on a frozen runtime bundle whose `PATH` publishes these same command names;
activating the checkout's venv over it would shadow them with the code being edited.
The root owns the formatter, lint, and ruff/pytest/pyright/dependency policy for every member, and the test gate for all of them:

- `./format.sh` — format and autofix the whole repository
- `run-tests --changed` — the pre-push gate, narrowed to what the diff can reach;
  the whole gate is the pull request's CI.
  The stages, the narrowing, and the opt-in and host-only stages: `local/AGENTS.md`, "Test gate"
- `sync-scripts --project <directory>` — regenerate a distribution's `[project.scripts]` and committed `_entrypoints.py`, then `uv sync --all-packages --all-groups --all-extras`
- `uv build --package <distribution>` — build a member's wheel;
  `benchmark/`'s is `uv build --directory benchmark`, since it is no member to name with `--package`

The development style policy is `dev/bro/prompts/dev/style.md`, tool-served to dev sessions as `dev-style-source::read`;
shell scripts follow `dev/bro/dev/shell_policy.py` (prelude sourcing, shebang), enforced repository-wide by `local/bro/local/shell_policy_test.py`,
and pass ShellCheck, which reads each file's dialect off its shebang and follows the sourced libraries through the root `.shellcheckrc`;
markdown prose follows the semantic line breaks of `dev/bro/dev/markdown_policy.py`, enforced repository-wide by `local/bro/local/markdown_policy_test.py` and checked over a reflow by `check-markdown`;
a test module's sleeps follow `dev/bro/dev/sleep_policy.py` (a yield, a poll interval in a loudly bounded loop, or a marked subject or bound), enforced repository-wide by `local/bro/local/sleep_policy_test.py`;
and code outside the credential resolver reads a credential by kind, never through the storage-addressed `get_instance` family, enforced repository-wide by `local/bro/local/credential_policy_test.py`.
Wheel contents are policy too (`dev/AGENTS.md`, "Wheel contents").

## Conventions

Framework code stays consumer-neutral:
what belongs to a consumer
— personas, credentials, task backends, toolsets
— reaches the framework through the entry-point groups (`bro/reference/extending.md`, "Entry-point groups"), and this checkout's own lives in `local/`.
Never stage a credential store or a synthesized secret directory.
Backward compatibility is not a design constraint:
the framework is in early beta with no external consumers to protect, so when the right design breaks an interface, moves a mechanism between distributions, or deletes a half-finished idea, that is the change to make
— "nothing existing changes" is no merit of a proposal;
the merit is the structure left behind.

A commit subject names its area, then states the change in the imperative, lowercase and without a trailing period: `trails: score the retirement soak by coverage`.
The area is the subsystem the change lands in
— a distribution or package directory (`ride:`, `trails:`, `bro/launch:` for a subpackage), the console script or spell it changes (`dive-in:`, `run-pr:`), `docs:` for a documentation-only change, `repo:` for a repository-wide one.
A body, in prose, carries what the subject cannot: the why, and the shape of the fix where it is not obvious from the diff.
A `Task:` line linking the task closes the message when the work has one;
what follows it is the hooks' own.
The token-accounting footer and the `Co-Authored-By` trailer are the hooks' (`dev/AGENTS.md`, "Commit metadata");
never hand-write or strip either.
