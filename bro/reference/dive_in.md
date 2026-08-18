# dive-in

`dive-in` is a task-oriented wrapper around `ride along`. It chooses a fresh workspace name, optionally prefetches a task, seeds the `fix` spell as the first prompt, and forwards the launch flags to the runtime. The wrapper is shipped by `bro-ride`.

## Modes

Exactly one mode applies:

- **bare** — no task flag; the optional positional command is the initial prompt;
- **task** — `-t / --task REF`; resolves and prefetches the task, then seeds `[[fix REF]]`;
- **new task** — `--new [seed]`; seeds `[[fix --new ""]]` or `[[fix --new <seed>]]`.

A positional command beside `--task` is appended as `Once you understand the task, <command>`. Task metadata, description, and comments are embedded in the launch prompt so the first turn does not race the session-local brog server.

## Workspace naming

Every invocation creates a fresh pinned workspace and passes it as `ride along --workspace NAME`:

- task mode uses the slugified task name;
- new-task mode uses the slugified seed or `dive-in-new`;
- bare mode uses `dive-in`.

Slugification lowercases, replaces non-alphanumeric runs with `-`, trims separators, and truncates to 40 characters. An empty task slug falls back to `dive-in`. A random suffix makes every name distinct.

The generated name is logged before launch. Use it with `ride exec` or `ride resume`.

## Base and hold defaults

With `--into` omitted, `dive-in` fetches origin's default-branch tip and forwards the resolved commit. If origin is unreachable it warns and leaves `--into` absent, so `ride along` falls back to the checkout's current `HEAD`. An explicit `--into REF` wins and skips the fetch.

An omitted hold is forwarded unset and takes `ride along`'s default: `attended` in a container, `guided` under `--host`. Host sessions have no container boundary, so the guided default retains permission prompts.

## Bro and launch flags

`dive-in` resolves the project's default bro itself when `--bro` is omitted, because `ride along` requires the bro positional. It forwards `--host`, `--hold`, `--grant`, `--revoke`, `--into`, `--no-trails`, `--harness` with every harness's own flags (claude's `--raw`), and the LLM-selection flags.

Task prefetch reads brog through the selected harness's prospective scope and the same grant/revoke values that the session receives. A missing backend or invalid scope override fails before a workspace is launched.

`-n / --dry-run` prints the final `ride along` command with shell quoting.

## Environment

- `RIDE_TASK_ID` is set to the canonical task id after a task is resolved. The PR workflow reads it when recording task attribution.
- `BRO_SHELL_COMMAND` preserves the user-facing `dive-in` invocation for the visual banner rather than exposing the generated `ride along` command.

The `RIDE_*` names remain intentionally unchanged until the dedicated runtime-state naming stage.
