# Design review: #849's tool fold and persona declarations, with #754

A throwaway copy of the design under review, never to be merged.
The task pages stay the source of truth:
[#849](https://github.com/dzhioev/bro/issues/849)'s `## Design`, whose parts this review touches are excerpted below,
and [#754](https://github.com/dzhioev/bro/issues/754)'s `## Design`, copied whole.
Once the review settles, both pages are updated in place and this branch is deleted.

## #849: excerpts of `## Design`

### The harness interface: the `serve` member

The framework half grows one slice at a time:

| Member | Replaces | Slice |
|---|---|---|
| `name` | the `Literal` and every list of names | #850 |
| `end_session`, `can_end_session` | `_claude_answer` and `_claude_raise`, the `RIDE_RUNNER_PID` check | service tools |
| `own_tools` | the job tools, `skill`, and their share of the `#tools` universe | service tools |
| `serve` | the Claude branches of `_fold_tool_layers`, the `bro`-harness special case in `_components_for` and `assemble` | tool fold, persona declarations |
| `facts` | the `#harness` forks left in prompts | prompts |

### Watching (#850): watches the model starts

- **Watches the model starts.**
  `bro::watch(command)` keeps an admitted command running for the rest of the session through the store, and `bro::unwatch(command)` stops it.
  Both mount wherever the persona declares shell reach, on every harness;
  on the bro harness they replace `job`'s `watch` mode.
  Under a finite roster `bro::watch` runs its command through brash, #754's interpreter, as `job` does, while `bro::unwatch` stops a watch without admitting its command, since stopping never widens reach.
  `bro::unwatch` refuses the session watch, which the runtime owns.
  A persona that declares no shell has no watches on either harness, although Claude still serves it an undeclared `Bash` until the persona slice.

### The slices after #850: the tool fold and the persona declarations

In the order they land, by dependency:
the tool fold and the persona declarations wait for #754's design and land together, so trails and prompts go first.

4. **Tool fold, with #754**, landing with the persona declarations.
   The fold produces a harness-neutral reach, and `Harness.serve(reach)` maps it:
   Claude onto the natives it passes through `--tools` and the shell it narrows behind the command gate, and the bro harness onto its own tools.
   General code reads two things off what `serve` returns:
   the server specs the harness mounts for the reach, such as the bro harness's `dev` toolset for `files`, and the groups the harness leaves unserved, which `bro show` lists.
   Claude's `--tools` and its gates are the Claude harness's own code, built from the same reach.
   #754's roster syntax and brash, the interpreter that runs a finite roster's lines, are general code:
   Claude's command gate rewrites each `Bash` and `Monitor` command into a brash call, and the bro job tool and `bro::watch` start brash where they start `bash -c` today.
   Claude serves `Bash` only where a `shell(...)` entry declares it.
   The persona's files level is an input to brash, which opens an output redirect only where files are writable;
   the harness writes it from the reach into the session's brash policy beside the roster when the session starts.
   The gate also denies a `Monitor` call that carries no command, its WebSocket form.
   Claude's own shell needs no pin, since it reads only brash's single-quoted call.
   The roster syntax, brash, and where it runs are #754's `## Design`.
   What a harness runs with is what the declaration and ride's own flags produce:
   `ride solo|along` takes no harness arguments after `--` on either harness, since one appended to Claude's argv could add `--tools`, MCP servers, plugins, settings, or a permission mode
   (repeated `--tools` lists union on 2.1.280).
   `bro/reference/ride.md`'s `-- --debug mcp` example goes with them.
5. **Persona declarations, with #857.**
   A persona declares its reach opt-in on every harness, in the `tools` vocabulary it uses today rather than a syntax of its own:
   the groups are constructors beside `mount` and `cli`, each returning a `ToolLayer` that carries a harness-neutral reach entry, composed with `when` and `iff` as any entry is.
   They are `files()`, read-write, and `files(write=False)`, read-only;
   `shell(...)`, which has this shape already, its roster syntax per #754;
   `web()`;
   and `delegation()`.
   `data_sources` folds into `tools`:
   a data source becomes a `source(...)` entry and a reference page a `man('<topic>')` entry, so a persona declares one list.
   Every entry has a key, its kind with a name, so entries of different kinds never share one, as a toolset namespace and a reference page's topic could:
   a group's name (`files`, `shell`, `web`, `delegation`);
   a mounted toolset's namespace;
   a data source's namespace;
   a reference page's topic, since the pages fold into one manual;
   a `cli` entry's generated tool name, since every `cli` tool serves under the one `cli` namespace;
   and for a raw server spec, the namespace it serves, which `MCPServerSpec` gains a field for so the key is known before anything is built.
   A layer carrying several entries, a raw `ToolLayer(server_specs=…)` or a `|` of layers, contributes each under its own key.
   Conditions select first, as today:
   a `when` or `iff` entry whose condition does not hold is omitted before keys resolve.
   A subclass's unmet `when(feature('x'), files(write=False))` thus leaves its base's `files()` in force, and a conditional `revoke(...)` withholds only where its condition holds.
   Then each key resolves along the Python MRO:
   the first class in it that declares the key decides it.
   That class's selected entries for the key reduce by kind:
   files levels to the wider, shell rosters to their union with `ANY` absorbing them, mounts of one toolset to the union of their tool subsets, and identical entries to one.
   Any other pair under one key fails the fold rather than letting list order decide:
   a `revoke` beside a grant, two different sources or raw specs, or two `cli` entries for one tool exposing different arguments.
   That is how a subclass narrows, as `files(write=False)` under an inherited `files()` or a shorter `shell(...)` roster.
   `revoke(...)` withholds the keys of the entries it wraps, whatever their level or subset, as in `revoke(delegation())`, `revoke(mount(brog_mcp.toolset))`, or `revoke(cli('bro list'))`;
   a descendant may grant them again:
   the nearest declaration of each key applies, a revoke included.
   The slice's tests cover each reduction and each refused pair;
   narrowing, a revoke and a regrant, and conditional grants and revokes over inherited entries;
   a subclass replacing one `cli` entry while keeping its sibling;
   and one key declared by two bases of a class that has several.
   `block`, `serve`, `allow_commands`, `claude.block`, and every `when(harness == …)` entry go, since nothing is on until declared.
   The base `bro` persona declares no group, so personas grant what they use rather than revoke what `Bro` grants, and the bare `bro` persona keeps only the loop tools on Claude.
   A harness serves a group with its own strongest tool where it has one and an equivalent elsewhere, and `bro show` lists a group a harness cannot serve as unserved there.
   Claude passes exactly the natives the groups map to through `--tools`, an allowlist, plus the loop tools every session gets.
   On the pinned 2.1.280:
   - `files(write=False)` maps to `Read`, `Glob`, `Grep`, and `LSP`, the code intelligence of the pyright plugin every session enables, which reads symbols, definitions, and references across the workspace;
     `files()` adds `Write`, `Edit`, and `NotebookEdit`.
     `Glob` and `Grep` are not in 2.1.280's default set, but serve where named.
     `LSP` fails on first use until sessions get the language server the plugin starts, #914.
   - `shell(...)` maps to `Bash`, `TaskStop`, and `Monitor`;
     `Bash` brings Claude's `GetTask`, which delivers a backgrounded command's result.
     2.1.280 withholds `Monitor` while telemetry is off, as the session's settings keep it (`DISABLE_TELEMETRY`);
     it stays mapped and gated for a version that serves it.
   - `web()` maps to `WebFetch` and `WebSearch`.
   - `delegation()` maps to `Agent`, `Workflow`, and `TaskStop`.
     A delegated agent gets at most the parent's natives, since the allowlist withholds the rest from the whole session:
     under `--tools Agent,Read`, the `general-purpose` agent, whose definition asks for every tool, had `Agent` and `Read` and no `Bash`.
   - The loop tools are `Skill`, since sessions rely on Claude's own skill loader, and `ToolSearch`, which `--tools` withholds unless named:
     it is what reaches the MCP tools that connect after an interactive session's first turn has started (`bro/reference/ride.md`, "Session-local MCP serving").
   - Where a persona declares no `files`, Claude serves `Read` behind a `PreToolUse` gate.
     It admits a path only when the path resolves, symlinks followed, inside one of the calling session's own Claude folders, both named from the hook's input:
     `claude-<uid>/<project>/<session id>/` under `CLAUDE_CODE_TMPDIR`, where a backgrounded command writes its output,
     and `<session id>/` beside the transcript, whose `tool-results/` holds the whole of a result too large to inline.
     Anything else is denied, and so is a path that does not resolve or a hook input the gate cannot read.
     A persona without `files` thus reads its own output whole and no workspace file through a tool.
     Claude's own customization, the repository's `CLAUDE.md` and project skills, still loads as in any Claude session, as the repository's instructions rather than reach.

   `--tools` drops a name the pinned binary does not serve without a word, as it drops `BashOutput`, `KillShell`, and `TaskOutput` on 2.1.280.
   So a test runs the pinned binary offline under the session's own settings with each registered persona's mapped names,
   `ENABLE_TOOL_SEARCH=true` standing in for the first-party API host,
   and holds its stream-json `init` event against the expected tools, aliases resolved (`Agent` reports as `Task`).
   `llm` probes hold the rest:
   a backgrounded command's output and an oversized result land under the read gate's roots, the gate denies a workspace file and a symlink out of those roots,
   and an `Agent` or `Workflow` child gets no native the parent's allowlist withholds.
   A Claude Code bump that renames a tool, moves those files, or widens a child fails them instead of changing a persona's reach unseen.
   Everything else is off, including the natives #857 lists, and natives a Claude Code bump adds stay off until mapped:
   on 2.1.280 that is `AskUserQuestion`, the plan-mode and worktree tools, `ScheduleWakeup`, `PushNotification`, the cron tools, `SendMessage`, `ListAgents`, `RemoteTrigger`, `Artifact`, `DesignSync`, `ReportFindings`, and the task-list tools.
   Claude runs with `--strict-mcp-config`, so the persona's own `--mcp-config` is its only MCP source:
   a repository's `.mcp.json` otherwise loads its servers beside the allowlist, which governs only natives,
   and the tools of claude.ai connectors stay disallowed by name (`mcp__claude_ai_*`) as well.
   The test above runs with a project `.mcp.json` in its workspace and holds that none of its tools appear.
   The bro harness serves files with the `dev` toolset, which moves into `bro-native`, and leaves web and delegation unserved, delegation until #863.
   Every registered persona declares the groups whose natives its Claude trails show it using, plus the groups its bro-harness declaration serves it today,
   by an inventory of every registered persona's Claude trails since 2026-09-01:
   - `dev` declares `files()`, `shell(ANY)`, `web()`, and `delegation()`, the last two for bro-dev's `WebFetch`, `WebSearch`, and `Agent` calls, so every dev descendant keeps them;
     `bro-dev` inherits them.
   - `eyebro` declares `files()`, `shell(ANY)`, and `web()`, which `bro-eyebro` inherits.
   - `analyst` and `terminal` declare `files()` and `shell(ANY)`, and `devoops` and `bro-watch-probe`, the conformance probes' bare bro, declare `shell(ANY)`.
   - `bro`, `lead`, and `browser` declare none of the four.
     The lead's and the browser's `Bash`, `Monitor`, and `TaskStop` calls in those trails ran the quest-watch commands the fold admitted before landing 1, which the runtime-owned session watch replaced.
   - A persona registered on master after this inventory takes its groups by the same rule when the slice lands.

   The natives the trails show going off are `AskUserQuestion`, which bro-dev called 57 times and the lead 7, as the interaction policy that puts questions in the turn's text already asks,
   and `Artifact`, `ScheduleWakeup`, `ExitPlanMode`, `SendFeedback`, and the cron tools, among those #857 lists.
   #870 (read-only and pure tags on tools) can then define the read-only files level by tag rather than by a list each harness keeps.
   `bro/harness/claude.py`'s tool names move into `ride/ride/claude/`.
   With no component conditioned on the harness, a bro has one selection, and the `bro`-harness special case in `_components_for` and `assemble` goes.
   #857 closes with this slice.

### Rollout and mixed versions

No landing changes a wire, store, or record format that two separately deployed processes share:

- One installation runs one revision:
  its distributions ship from one commit (`BOOTSTRAP.md`), and `ride` freezes the whole installation into a runtime bundle.
- Every party tree runs its root's bundle, spawned and manual children included, and a resume runs the bundle its workspace recorded, so old and new code never meet in one session tree;
  roots started before an install keep their bundle.
- The watch store, the waiter's registration, and the stand-down mark live in one session's state dir and are read only by that session's own processes.
  Hooks travel in each launch's `--settings`, never in the workspace's Claude state, so a resumed workspace gets the hooks of the bundle it runs.
- The trails server is the one separately deployed reader:
  an ECS service whose image installs only the core `bro` wheel, deployed through the `trails-server` target in `oops/deploy_targets.py`.
  No landing changes what a trail records or what the server serves, so either side may run the older revision;
  the rewake reaches a Claude transcript as records the projection already handles, an unknown attachment as a notification and a user record as input, which the `llm` stage's input-channel probe holds.
  The trails landing changes the server's own code, so the server redeploys after it merges.
- A trail format a harness distribution adds later reaches the trails server first:
  the distribution goes into the server's image and deploys before any installation records in that format, since the server refuses an unknown format at blaze and a session whose recording fails stops.
  The server's image keeps installing that distribution while any retained trail uses the format, whatever the recording installations carry, so retiring a format waits until no recorder writes it and its trails are gone.
- The benchmark launcher reads harness names off the manifest of the bundle it builds rather than its own interpreter.
  A bundle built before the registry carries the old manifest format, which the launcher refuses as stale, naming `benchmark bundle` to rebuild it, so a new launcher never reads an old bundle as carrying no harness.
- A repository that pins the framework adopts each landing through its own bump, which adapts its persona declarations and prompts, and runs the revision it pins until then.
- Landing 5 changes no contract between separately deployed processes either.
  `--tools`, the gates' arguments, and the brash policy are built at each launch from the bundle the session runs, so a root started before the install, its children, and a workspace it resumes keep their reach until a fresh root.
  brash's `tree-sitter` pins join `bro`'s `runtime` extra, `bro-ride`, and `bro-native`, never the `trails-server` extra the trails server's image installs, so the server neither changes nor redeploys;
  the benchmark project relocks for `bro-ride`'s new pins.
  A repository that pins the framework fails on the removed constructors (`block`, `serve`, `allow_commands`, `claude.block`) until its bump moves its declarations to the reach groups.
  A workspace record written before the install carries the session's `arguments`, always empty there, since the record is the resume variant:
  the new loader drops that field while it is empty and refuses it otherwise, so `ride list` keeps reading those workspaces,
  and `ride resume` re-executes the recorded bundle before it loads any record (`recorded_runtime_reference`).

Each landing goes live by installing the launcher from master and starting a fresh root;
the trails landing deploys the trails server between the two.

### Risks: the persona slice

- Opt-in natives can withhold a tool a persona relied on without naming it, such as LSP or plan mode;
  the persona slice inventories the natives before switching, and `bro show` lists each persona's reach per harness.
- The native names and the read gate's roots are Claude Code's own and undocumented;
  the mapping test and the `llm` probe hold them against the pin, and a bump reruns both.

### Rejected: the reach declarations

- **A syntax of its own for the reach groups:**
  settled with the user for constructors in the `tools` vocabulary, which already composes conditions and layers.
- **A final `revoke`, as `False` is for a feature:**
  settled with the user for one rule, the nearest declaration of each key.
  A subclass, in its base's distribution or another one, is trusted code that could do whatever its base withholds, so finality would guard no boundary;
  the session's permission document and the harness's enforcement are what limit a session.
- **The tool fold as a landing before the persona declarations:**
  the revision released between them would carry the old declarations over the new fold, a state nobody designed, and no rollout step needs to sit between them.
- **`LSP` among the loop tools:**
  it reads the workspace, so a persona without `files`, the browser among them, would read the workspace through it.
- **Leaving `ToolSearch` out:**
  `--tools` withholds it unless named, and an interactive session's first turn reaches its late-connecting MCP tools through it.
- **`Read` only with `files`:**
  a persona without `files` would see only a preview of an oversized result and none of a backgrounded command's output until it ends, since 2.1.280 serves both as files;
  settled with the user for the gated `Read`.
- **`shell(...)` implying `files(write=False)` on Claude:**
  under a finite roster it would grant the reads the roster withholds.
- **Refusing only the forwarded flags that shape reach:**
  settled with the user for no forwarded harness arguments at all, so no list of Claude's flags needs keeping current.
- **Withholding `Skill` from personas without `files`:**
  `CLAUDE.md` would still load, and only `--bare` skips it, which also drops the hooks a session runs on;
  settled with the user for naming the customization channels as outside reach.
- **Provisioning the LSP server in this landing:**
  sessions lack it today, whatever the mapping;
  settled with the user for #914.
- **Declaring by reach**, every group a persona does not block today:
  personas would keep groups their trails never touch, the lead's `web` among them;
  settled with the user for declaring by use.

### Landings: the fifth

5. Tool fold and persona declarations, with #754 and #857, after #754's design:
   the fold's neutral reach and the declarations that produce it change together, so no released revision carries the old declarations over the new fold.

## #754: `## Design`

Settled with the user on 2026-10-08 in the resumed design session (trail `01m4d6xtmb-51hy9xrz-hydyryva`), against master at `99cd42a2`, as the prerequisite of #849's landing 5.
Revised in the design review of PR #912, also on 2026-10-08:
a finite roster's lines run in brash, the framework's own interpreter, rather than in the host's bash.
The walk below comes from a prototype run against bash 5.2.37 with `tree-sitter` 0.26.0 and `tree-sitter-bash` 0.25.1.

### Scope: policy, not a boundary

A finite `shell(...)` roster states what a persona is meant to run and catches a model that goes off-script, the same way on every harness.
It is not a boundary against a model, or hostile data, trying to get past it:
a roster that names a general-purpose program (`git -c …`, `uv run`, `find -exec`, `xargs`, `env`, a shell, an interpreter) admits whatever that program runs, and brash runs on the host without containment.
Containment is #816's sandbox.
A command that must be held to exact arguments is served as a `cli(...)` tool, a fixed argv with no shell, which #493 extends to external commands.
The reference docs say so where they describe `shell(...)`, and state what a finite roster leaves open on Claude:

> Under a finite roster, brash checks the commands the model runs through `Bash` and `Monitor` on Claude and through its jobs and watches on the bro harness.
> What the session's Claude configuration runs is not checked: hooks from your or the repository's settings, plugins, and a skill's inline `!cmd` lines, which Claude runs without consulting any `PreToolUse` hook.
> So a model that can write files can get around its roster: it writes a skill whose `!cmd` runs what it likes, and the skill takes effect from the next session.
> Claude's own guard against that is a permission prompt, which ride skips under every hold but `guided`; even Claude's auto mode let a benign-looking skill write through when probed.
> The roster catches a model that goes off-script, not one working to get around it.
> A finite-roster session refuses to start while its settings, the skills Claude would load, or its enabled plugins carry a `PreToolUse` hook matching `Bash` or `Monitor`, which could rewrite their calls after the gate.

Each claim there was probed live on 2.1.280:
a model limited to `Write` and `Skill` wrote such a skill with no prompt, the next session loaded it, and its `!cmd` ran beside a `PreToolUse` hook on `Bash` that denied everything and never saw it;
with `Bash` withheld, the same `!cmd` was refused, since Claude checks it against `Bash`'s permission rules.

### Roster syntax

- An entry is written the way the command is written and split into words with shell quoting:
  `shell('git log ...', 'gh pr view *', 'gh pr view * --json *', 'rewind show *', 'NO_COLOR=1 gh ...')`.
- Each word matches the argument at its position.
  A `*` inside a word matches any text within that one argument, options included (`--since=*`);
  a trailing `...` admits any further arguments, none included.
  An entry with neither admits exactly that command.
- The entry's own quoting marks what is literal, as bash's does:
  a quoted `*` matches a star, so `grep -E 'a*b' ...` admits only that pattern, and a quoted `'...'` matches three dots;
  only an unquoted `*` or a final unquoted `...` is a pattern.
- Leading `NAME=value` words declare prefix assignments, matched the same way, so `NO_COLOR=1 gh pr list` needs an entry that spells the prefix.
- Declaration refuses an entry that holds shell operators (an entry names one simple command), a `...` anywhere but last, and a program word that is not literal.
  It refuses a program word naming a brash builtin, which needs no entry, and one naming a bash builtin brash does not implement (`eval`, `source`, `read`, …), which nothing could run.
- `ANY` stays unrestricted, and rosters under one key reduce to their union with `ANY` absorbing them, as #849 fixes.

### brash

`bro/brash.py`, with its console script `brash`, runs a finite roster's command lines on both harnesses, in place of `bash -c`;
`bro/shell.py`'s exact-match `admit_command` goes.
Its inputs are the line, the roster and the persona's files level, both from a policy file the session writes when it starts, and its own environment, which the commands it starts inherit.
It imports nothing of the framework beyond itself, since every line pays for its start:
about 65 ms in a container, the parse included, against 22 ms for Python alone.

- **Parser.** `tree-sitter-bash` through the `tree-sitter` Python bindings, both MIT and pinned.
  Core's `runtime` extra pins them, and so do `bro-ride` and `bro-native`, whose sessions start brash.
- **Language.** brash runs the part of bash the prototype's walk admitted, with bash's meaning for it:
  pipelines, `&&`, `||`, `;`, `&`, `!`, subshells and groups, `if`, `for … in`, `while`, `until`, `case`, comments, plain assignments, redirects, here-documents and here-strings, and the words below.
  Any other node kind, a parse error or a missing node, or text the grammar left unparsed where bash would expand it refuses the whole line before any of it runs;
  `tree-sitter-bash` leaves a backtick in an unquoted heredoc body unparsed, for one.
- **Words.** A word is literal text, quoting, `$name`, `${name}`, the special parameters, `$(…)`, which brash runs itself, process substitution, a `$'…'` string, a tilde prefix, globs, and brace patterns.
  An unquoted result splits and globs as bash's does.
  Parameter-expansion operators beyond `${name}`, backticks, arithmetic, arrays, and `$"…"` are not implemented, and refuse the line with the implemented form suggested.
- **Builtins.** brash implements `cd`, `pwd`, `echo`, `exit`, `true`, `false`, `:`, and a plain `wait`, which need no entry.
  Every other command is a program found on `PATH`, `test`, `[`, `printf`, and `kill` among them, and needs an entry.
  None of bash's other builtins exists in brash, so `eval`, `source`, `exec`, `read`, `export`, `declare`, `set`, and the rest are refused as unknown commands,
  and the name-binding and evaluating options bash's builtins carry (`printf -v`, `wait -p`, `read -a`, `declare -n`) have nothing to run in.
- **The checks.** Before running anything, brash checks every command whose program word is literal against the roster.
  As it starts each command, it checks the real argv, after expansion, splitting, and globbing, so `rewind show $T` runs while `$T` is one word that `rewind show *` admits and is refused once it splits.
  A refused command stops the line there with the status bash gives a command it cannot run, 126, and the refusal below on standard error.
- **Variables.** A plain assignment or a `for` variable sets a brash variable, admitted only when its name is not in brash's environment,
  so `x=$(git merge-base a b); git diff "$x"` works and `PATH=…; git …` does not.
  brash looks programs up with its environment's `PATH`.
  Prefix assignments are part of an entry.
- **Redirects.** brash classifies every redirect form the grammar produces by the open it performs, and refuses any form it does not implement:
  - always admitted: here-documents, here-strings, duplicating or closing a descriptor (`2>&1`, `<&0`, `>&-`), and `/dev/null`, `/dev/stdout`, or `/dev/stderr` as a target;
  - reading a file (`<`, `n<`): admitted from any path, into a command the roster admits;
  - writing a file (`>`, `>>`, and `>|`, each with or without a descriptor number before it, and `&>`, `&>>`, and a bare `>&` onto a file, which take none): admitted only where the persona's files reach is writable (`files()`).

  A `>&` or `<&` duplicates or closes when its word is a descriptor number or `-`, and only a bare `>&` with any other word writes a file;
  one with a descriptor number before it and any other word is refused, as bash refuses it (`2>& out` is an ambiguous redirect).
  A number before `&>` or `&>>` is the command's argument, as in bash, where `echo 3&> out` writes `3` to `out`.
  The read-write `<>`, which the pinned grammar reads only with a parse error, is refused with it, and so is the `{name}>` fd-variable form.
  brash opens every target itself, after expansion, as a file, so bash's `/dev/tcp/…` and `/dev/udp/…` are ordinary paths there, which no system has.
  A command of redirects alone is not implemented:
  bash's `$(< file)` reads a file with no program, which no entry would declare, so `echo "$(< /workspace/secret)"` is refused.
  The unit tests take each operator under each files level, that case among them.
- **The refusal.** One message on both harnesses names the refused command or construct, why it was refused, and the roster's entries, and suggests the admitted form where there is one:
  quoting an expansion, `$(…)` for a backtick, an entry to declare.

### Where it runs

- **Bro harness.** The job tool and `bro::watch` start a finite roster's line as `brash -c '<line>'` under the job supervisor, where they start `bash -c` today.
  `bro::unwatch` stops the watch whose command it names without admitting it, since stopping never widens reach.
- **Claude.** The command gate, `ride/ride/claude/watch_guard.py` renamed `command_gate.py`, is the `PreToolUse` hook on `Bash` and `Monitor` wherever the roster is finite.
  Through `updatedInput`, which 2.1.280's hook schema carries, it rewrites the call's command into one POSIX-shell argv:
  the runtime's `brash` by path, `-c`, and the model's line untouched, quoted as `shlex.join` quotes them, with the call's other input fields kept as they are.
  The model writes ordinary lines, and Claude's own shell, bash or zsh, reads only single-quoted words, which every version of either reads alike, whatever the line holds.
  It denies a `Monitor` call that carries no command, its WebSocket form.
  The gate stays stdlib-only, since brash does the checking.
  The line no longer meets Claude's shell snapshot, whose functions and aliases belong to Claude's shell, not to brash.
  Before Claude starts, the runner refuses a finite-roster session whose project or local settings, skills Claude would load, or enabled plugins carry a `PreToolUse` hook matching `Bash` or `Monitor`, naming where it found it,
  since Claude applies every matching hook's `updatedInput` and the last rewrite wins.
- **`ANY`.** brash does not run:
  Claude serves `Bash` ungated, and the job tool and `bro::watch` start `bash -c`.
- **`cd`** inside a line moves brash for the rest of that line only.
  On Claude, under a finite roster, it no longer carries to the next call, as on the bro harness, where every job starts afresh.

### Verification

- **Unit tests** for each construct brash runs and each it refuses, both checks, redirects against the files level, variables, the refusal message, entry parsing and its declaration errors, and the union under `ANY`.
- **Against bash, by difference.** The corpus, the lines the spells prescribe, everyday model commands, and every hole found by hand, runs under brash and under bash 5.2.37 with stub programs on `PATH` that log their argv and stdin.
  For every line brash runs, both start the same argvs with the same stdin and leave the same output and status.
  Bash is the reference here, not a dependency.
- **By generation.** A generator over brash's grammar nests sentinel commands and single-quoted payloads such as `a[$(sentinel)]` in every construct and quoting context.
  It holds that brash starts no program its roster does not admit, on every path that starts one:
  pipelines, substitutions, process substitutions, background jobs, conditions, and loops.
- **Integration.** Against the pinned Claude, the gate rewrites `Bash` and `Monitor` calls into brash, a refused command's message reaches the model, and the WebSocket form is denied.
  Lines holding single quotes, newlines, substitutions, and operators round-trip through the rewrite, and Claude's shell starts only brash;
  the job tool and `bro::watch` run lines through brash and refuse the same ones.

### The contract with #849

- **Fits.** The roster syntax and brash are general code in core, which Claude's command gate rewrites into and the bro harness's job tool and `bro::watch` start;
  Claude serves `Bash` only where `shell(...)` declares it;
  `shell(...)` is a reach constructor whose rosters reduce to their union, with `ANY` absorbing them.
- **Adds to slice 4.** The persona's files level is an input to brash, so the harness writes it from the reach into the session's brash policy beside the roster.
  The gate rewrites `Bash` and `Monitor` into brash and denies `Monitor`'s WebSocket form.
  No shell pin is needed, since Claude's shell only reads brash's single-quoted call.
- **Changes one line of the watching design.** `bro::unwatch` stops a watch without admitting its command.
- **Personas.** Landing 5 keeps today's `shell(ANY)` declarations.
  Narrowing a persona is its own change, once its command inventory is known.

### Edge cases

- `git range-diff $(git merge-base a b)..b a..c` is admitted through `git ...`, with its `git merge-base` checked too;
  `gh pr view 12 --json t | jq .t` needs a `jq` entry.
- `git commit -m "$(cat <<'EOF' … EOF)"` runs `cat`, which needs an entry;
  `git commit -F - <<'EOF'` does not.
- `rewind show "$T"` matches `rewind show *`, and so does `rewind show $T` while `$T` is one word.
- `[[ -f x ]]` is refused;
  `[ -f x ]` needs an entry for `[`, and runs the system's `[`.
- `cmd > out.txt` and `cmd > "$out"` run where files are writable, and neither does elsewhere.
- An entry naming a relative path (`./x.sh`) admits whatever that path names in the current directory, which `cd` changes freely.
- An entry naming a shell or an interpreter (`bash ...`, `python ...`) admits whatever it runs, as any general-purpose program does.
- A hostile value can still become an argument of a roster program (`gh pr view "$x"` with `x=--web`);
  what a program does with its arguments is the program's.

### Risks

- brash runs a part of bash:
  a model's habits beyond it (`[[ ]]`, `$(( ))`, `${x:-…}`, backticks, `export`) cost it a refusal that names the admitted form, and a persona declares the programs it reads with (`cat`, `ls`, `test`).
  The differential tests keep what brash runs the same as bash.
- Every line pays about 65 ms to start brash, and the gate's own start on Claude.
- `cd` no longer carries between Claude's calls under a finite roster.

### Rejected

- **A boundary in this task.** brash checks what it starts, but a roster program runs what it likes and nothing contains the host;
  Claude Code's own analyzer is documented as no boundary either.
- **Bash as the executor, behind a static matcher**: what the matcher admits holds only for the bash it was tested against, and a host's bash ranges from macOS's 3.2 to 5.3;
  holding them to one reading meant testing every version or shipping one bash.
  Settled with the user for brash.
- **Shipping a bash**: per-platform binaries to host or fetch, with Debian's static build on Linux and nothing equivalent on macOS;
  settled with the user for brash.
- **A wrapper the model writes itself** (`brash -c '…'`):
  a model forgets it and loses a turn to each refusal, so the gate rewrites through `updatedInput` instead.
- **A lexer of our own** and **argv-only matching**, which loses variables, pipes, and `$(…)`.
- **`mvdan.cc/sh`'s interpreter**:
  Go in a Python repository, a binary to host or fetch, and its handlers documented as no isolation.
  **Its parser, through `shfmt --to-json`**:
  a Go binary from a third-party wheel and a subprocess per check.
  **bashlex**:
  GPL-3.0, which the Apache-2.0 framework cannot depend on.
- **Claude's own `Bash(...)` rules**, which the original proposal translated rosters into:
  under `--dangerously-skip-permissions` they cannot express an allowlist, and they would be a second matcher with semantics of their own.
- **Enforcement in Claude's shell prefix**:
  the prefix sees Claude's wrapper (snapshot sourcing, `eval`, cwd tracking) rather than the model's line.
- **`*` across arguments, or prefix-only entries**:
  no entry could hold a command to one argument.
- **Unwrapping wrappers** (`timeout`, `nice`, `env`) the way Claude Code does:
  more surface for a convenience an entry gives.
- **Only ride's own Claude configuration under a finite roster** (`--setting-sources user`, `Skill` withheld, skills' inline shell off):
  it closes the configuration channels, but drops the repository's `CLAUDE.md` and skills too, which 2.1.280 stops loading under that flag, and the user's own `!cmd` skills;
  settled with the user for stating the hole instead.
- **Claude's auto permission mode instead of skipped prompts**:
  it exists only on the Claude 5 models, so a `haiku` recipe falls back to the default mode, where an unattended run is denied every write;
  its classifier let a benign-looking skill write through, and every action waits on that classifier, which blocks when unavailable.
