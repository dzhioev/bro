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
  `bro::watch` admits its command against the persona's shell roster, as `job` does, while `bro::unwatch` stops a watch without admitting its command, since stopping never widens reach.
  `bro::unwatch` refuses the session watch, which the runtime owns.
  A persona that declares no shell has no watches on either harness, although Claude still serves it an undeclared `Bash` until the persona slice.

### The slices after #850: the tool fold and the persona declarations

In the order they land, by dependency:
the tool fold and the persona declarations wait for #754's design and land together, so trails and prompts go first.

4. **Tool fold, with #754**, landing with the persona declarations.
   The fold produces a harness-neutral reach, and `Harness.serve(reach)` maps it:
   Claude onto the natives it passes through `--tools` and the shell it narrows behind the command gate, and the bro harness onto its own tools, refusing native names.
   #754's shell syntax and its matcher are general code that the Claude gate, the bro job tool, and `bro::watch` call, and Claude serves `Bash` only where a `shell(...)` entry declares it.
   The persona's files level is an input to the matcher, which admits an output redirect to a file only where files are writable:
   `Harness.serve(reach)` hands it to the gate, and the bro harness reads it beside the roster.
   Under a finite roster, Claude's session shell is bash, since the matcher reads bash grammar, and the gate also denies a `Monitor` call that carries no command, its WebSocket form.
   #754's pattern syntax and enforcement mechanism are settled in #754's own design, resumed before this slice starts.
5. **Persona declarations, with #857.**
   A persona declares its reach opt-in on every harness, in the `tools` vocabulary it uses today rather than a syntax of its own:
   the groups are constructors beside `mount` and `cli`, each returning a `ToolLayer` that carries a harness-neutral reach entry, composed with `when` and `iff` as any entry is.
   They are `files()`, read-write, and `files(write=False)`, read-only;
   `shell(...)`, which has this shape already, its roster syntax per #754;
   `web()`;
   and `delegation()`.
   `data_sources` folds into `tools`:
   a data source becomes a `source(...)` entry and a reference page a `man('<topic>')` entry, so a persona declares one list.
   Every entry has a key:
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
   Claude passes exactly the natives the groups map to through `--tools`, an allowlist, plus the loop tools the slice lists:
   among them `Skill`, since sessions rely on Claude's own skill loader, and the `LSP` tool the pyright plugin every session enables needs.
   `ToolSearch` is not what keeps ride's MCP tools reachable, since they load eagerly (`alwaysLoad`);
   leaving it out only stops Claude deferring its own tools.
   Everything else is off, including the natives #857 lists, and natives a Claude Code bump adds stay off until mapped.
   The bro harness serves files with the `dev` toolset, which moves into `bro-native`, and leaves web and delegation unserved, delegation until #863.
   Every registered persona declares the groups it reaches today, settled by the slice's inventory of the natives each one uses:
   `terminal` without delegation, which it blocks, and `browser` with none of the four.
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

Each landing goes live by installing the launcher from master and starting a fresh root;
the trails landing deploys the trails server between the two.

### Risks: the persona slice

- Opt-in natives can withhold a tool a persona relied on without naming it, such as LSP or plan mode;
  the persona slice inventories the natives before switching, and `bro show` lists each persona's reach per harness.

### Rejected: the reach declarations

- **A syntax of its own for the reach groups:**
  settled with the user for constructors in the `tools` vocabulary, which already composes conditions and layers.
- **A final `revoke`, as `False` is for a feature:**
  settled with the user for one rule, the nearest declaration of each key.
  A subclass, in its base's distribution or another one, is trusted code that could do whatever its base withholds, so finality would guard no boundary;
  the session's permission document and the harness's enforcement are what limit a session.
- **The tool fold as a landing before the persona declarations:**
  the revision released between them would carry the old declarations over the new fold, a state nobody designed, and no rollout step needs to sit between them.

### Landings: the fifth

5. Tool fold and persona declarations, with #754 and #857, after #754's design:
   the fold's neutral reach and the declarations that produce it change together, so no released revision carries the old declarations over the new fold.

## #754: `## Design`

Settled with the user on 2026-10-08 in the resumed design session (trail `01m4d6xtmb-51hy9xrz-hydyryva`), against master at `99cd42a2`, as the prerequisite of #849's landing 5.
The evidence below comes from a prototype of the walk, run against bash 5.2.37 with `tree-sitter` 0.26.0 and `tree-sitter-bash` 0.25.1.

### Scope: policy, not a boundary

A finite `shell(...)` roster states what a persona is meant to run and catches a model that goes off-script, the same way on every harness.
It is not a boundary against a model, or hostile data, trying to get past it:
no static reading of bash can be shown complete, and a roster that names a general-purpose program (`git -c …`, `uv run`, `find -exec`, `xargs`, `env`) admits whatever that program runs.
Containment is #816's sandbox.
A command that must be held to exact arguments is served as a `cli(...)` tool, a fixed argv with no shell, which #493 extends to external commands.
The reference docs say so where they describe `shell(...)`.

### Roster syntax

- An entry is written the way the command is written and split into words with shell quoting:
  `shell('git log ...', 'gh pr view *', 'gh pr view * --json *', 'rewind show *', 'NO_COLOR=1 gh ...')`.
- Each word matches the argument at its position.
  A `*` inside a word matches any text within that one argument, options included (`--since=*`);
  a trailing `...` admits any further arguments, none included.
  An entry with neither admits exactly that command.
- Leading `NAME=value` words declare prefix assignments, matched the same way, so `NO_COLOR=1 gh pr list` needs an entry that spells the prefix.
- Declaration refuses an entry that holds shell operators (an entry names one simple command), a `...` anywhere but last, a program word that is not literal, and a program that the walk admits without an entry or always refuses (below).
- `ANY` stays unrestricted, and rosters under one key reduce to their union with `ANY` absorbing them, as #849 fixes.

### The matcher

`bro/shell.py`, which holds the exact-match `admit_command` today, admits or refuses one command line.
Its inputs are the line, the roster, whether the persona's file reach is writable, and the names exported in the environment the line will run in.

- **Parser.** `tree-sitter-bash` through the `tree-sitter` Python bindings, both MIT and pinned, imported only when a line is admitted, so declaring a persona stays light.
  Core's `runtime` extra pins them, and so do the members whose processes admit commands:
  `bro-ride` for the gate and `bro-native` for the job tool.
- **The walk.** An allowlist over the tree:
  pipelines, `&&`, `||`, `;`, `&`, `!`, subshells and groups, `if`, `for … in`, `while`, `until`, `case`, comments, assignments, redirects, and the word forms below.
  It collects every simple command, including those inside `$(…)`, `<(…)`, `>(…)`, conditions, and loop bodies, and checks each one against the roster.
  Any other node kind, a parse error or a missing node, or text the grammar left unparsed where bash would expand it refuses the whole line;
  `tree-sitter-bash` leaves a backtick in an unquoted heredoc body unparsed, for one.
- **Words.** A word evaluates to literal text with unknown segments where expansions are.
  The admitted expansions are `$name`, `${name}`, the special parameters, and `$(…)`.
  A double-quoted expansion, a `$(…)` inside double quotes, a process substitution, a `$'…'` string, or a tilde prefix is one unknown argument, matched only by a `*` that covers it.
  An unquoted expansion, glob, or brace pattern may become any number of arguments, so only a trailing `...` covers it.
- **Parameter-expansion operators and backticks are refused.** That means every `${…}` form beyond `${name}` (`${x:-…}`, `${x/…}`, `${x#…}`, `${x:offset}`, `${!x}`, `${#x}`, `${x@…}`, …) and every backtick substitution.
  Inside them `tree-sitter-bash` and bash disagree on quoting, so a `$(…)` that bash runs reads as inert text:
  on the prototype, a generator that nested a sentinel command in these forms found 92 admitted lines that ran it, and none once the word language was cut to the forms above.
- **Arithmetic and evaluated names are refused.** That means `$(( ))`, `(( ))`, C-style `for`, arrays and array subscripts, and the nameref and integer attributes.
  Bash evaluates text there as an expression or a variable name, so a value such as `a[$(cmd)]` runs `cmd` even from single quotes.
- **Builtins and keywords.** Every name `compgen -b` and `compgen -k` print for the tested bash is classified:
  - admitted without an entry, their arguments free:
    `:`, `true`, `false`, `cd`, `pwd`, `echo`, `exit`, `return`, `break`, `continue`, `shift`, `wait`;
  - admitted only through an entry, with their evaluating forms refused even then:
    `test` and `[` (a word bash could read as an operator is literal, and never `-v` or `-R`),
    `printf` (a `-v` name is a literal identifier),
    `read` and the declaration builtins `export`, `declare`, `typeset`, `local`, `readonly`, `unset` (literal identifiers; no `-n`, `-i`, `-a`, `-A`),
    and `kill`, `umask`, `ulimit`, `pushd`, `popd`, `dirs`, `type`;
  - refused, at declaration too:
    every other one, among them those that run text as code (`eval`, `source`, `.`, `exec`, `command`, `builtin`, `trap`, `let`, `mapfile`, `readarray`, `getopts`, `time`, `coproc`),
    those that change how the shell reads what follows (`set`, `shopt`, `alias`, `enable`, `hash`),
    the interactive ones, and the keywords `select`, `function`, and `[[ ]]`;
  - a program name that holds a `/` is a path, never a builtin.
- **Variables.** A plain assignment or a `for` variable is admitted when its name is not exported in the environment, so `x=$(git merge-base a b); git diff "$x"` works and `PATH=…; git …` does not.
  Prefix assignments are part of an entry, and the declaration builtins need an entry of their own.
  Reading a variable is free.
- **Redirects.** Here-documents, here-strings, fd duplication (`2>&1`), and `/dev/null`, `/dev/stdout`, `/dev/stderr` are always admitted.
  Any other target is a literal path, a tilde prefix allowed, or a process substitution, which is walked.
  A target bash opens as a socket (`/dev/tcp/…`, `/dev/udp/…`) is refused, and so is a target an expansion computes, since its value could be one.
  Output to a file (`>`, `>>`, `&>`, `>|`) is admitted only where the persona's file reach is writable (`files()`), as Claude Code checks a redirect against its file-edit rules.
  The `{name}>` fd-variable form is refused.
- **Other refusals.** Function definitions, `$"…"`, control characters other than tab and newline, and a backslash-newline outside quotes, where `tree-sitter-bash` splits the word that bash joins.
- **The refusal.** One message on both harnesses names the refused simple command or construct, why it was refused, and the roster's entries, and suggests the admitted form where there is one:
  quoting an expansion, `$(…)` for a backtick, a literal redirect target.

### Where it runs

- **Bro harness.** The job tool and `bro::watch` call the matcher before starting the line under `bash -c`, as they call `admit_command` today, with the environment of the admitting process, which the line inherits.
  `bro::unwatch` stops the watch whose command it names without admitting it, since stopping never widens reach.
- **Claude.** The command gate, `ride/ride/claude/watch_guard.py` renamed `command_gate.py`, is the `PreToolUse` hook on `Bash` and `Monitor` wherever the roster is finite.
  Its argv carries the roster and the file-reach level, and it reads the exported names from its own environment, which the command's shell also inherits from Claude.
  It denies a refused line with the message above, and a `Monitor` call that carries no command, its WebSocket form.
  A finite roster pins the session's shell to bash, since the matcher reads bash grammar;
  `ride/ride/claude/shell_prefix.py` takes zsh from `SHELL` today.
  The line runs as written:
  nothing is rewritten through `updatedInput`.
- **`ANY`.** No matcher runs:
  Claude serves `Bash` ungated, and the job tool and `bro::watch` admit any line.

### Verification

- **Unit tests** for each rule:
  entry parsing and its declaration errors, word evaluation, each refusal, the refusal message, and the union under `ANY`.
- **Against bash, by corpus.** The lines the spells prescribe, everyday model commands, and every hole found by hand run under the real bash with no programs on `PATH`,
  where `command_not_found_handle` logs each argv bash starts, and with hostile values in the variables the lines read.
  The test fails when bash starts an argv that no simple command the walk admitted could produce.
- **Against bash, by generation.** An exhaustive generator over a bounded grammar of the admitted constructs (every builtin, keyword, word form, quoting context nested in another, redirect, and here-document)
  places sentinel commands and single-quoted payloads such as `a[$(sentinel)]` in every position.
  The test fails when the walk admits a line under which bash starts a sentinel.
  It found 92 such lines on the prototype's first walk, and it reruns on every bump of bash, `tree-sitter`, or the grammar.
- **Classification.** The test fails when `compgen -b` or `compgen -k` prints a name the matcher does not classify.
- **Integration.** The gate denies on `Bash` and `Monitor` and passes a match;
  the job tool and `bro::watch` refuse and admit the same lines;
  a finite roster runs Claude's commands in bash.

### The contract with #849

- **Fits.** The syntax and the matcher are general code in core, which the Claude command gate, the bro harness's job tool, and `bro::watch` call;
  Claude serves `Bash` only where `shell(...)` declares it;
  `shell(...)` is a reach constructor whose rosters reduce to their union, with `ANY` absorbing them.
- **Adds to slice 4.** The persona's files level is an input to the matcher, so `Harness.serve(reach)` hands it to the gate and the bro harness reads it beside the roster.
  Under a finite roster, Claude's session shell is bash and the gate also denies `Monitor`'s WebSocket form.
- **Changes one line of the watching design.** `bro::unwatch` stops a watch without admitting its command.
- **Personas.** Landing 5 keeps today's `shell(ANY)` declarations.
  Narrowing a persona is its own change, once its command inventory is known.

### Edge cases

- `git range-diff $(git merge-base a b)..b a..c` is admitted through `git ...`, with its `git merge-base` checked too;
  `gh pr view 12 --json t | jq .t` needs a `jq` entry.
- `git commit -m "$(cat <<'EOF' … EOF)"` runs `cat`, which needs an entry;
  `git commit -F - <<'EOF'` does not.
- `rewind show "$T"` matches `rewind show *`;
  `rewind show $T` does not, and the refusal says to quote it.
- `[[ -f x ]]` is refused;
  `[ -f x ]` needs an entry for `[`.
- `cmd > out.txt` is admitted where files are writable;
  `cmd > "$out"` is refused.
- An entry naming a relative path (`./x.sh`) admits whatever that path names in the current directory, which `cd` changes freely.
- Claude sources a shell snapshot before each command;
  the matcher reads the line, not the functions or aliases the snapshot defines.
- A hostile value can still become an argument of a roster program (`gh pr view "$x"` with `x=--web`);
  what a program does with its arguments is the program's.

### Risks

- A finite roster refuses habitual commands and constructs (`cat`, `ls`, `[[ ]]`, `$(( ))`, `${x:-…}`, backticks);
  the refusal names the roster and the admitted form, and a persona declares the readers it wants.
- The corpus and generated tests hold for the bash they run;
  a boxed session runs the runtime image's bash, and an unboxed one the host's, which may be older or newer.
- The gate costs about 70 ms per call, against about 30 ms for today's exact match, as measured on the prototype.
- `tree-sitter` 0.26.0 ships no wheel for Intel macOS, where installing builds it from source.

### Rejected

- **A boundary in this task.** No static reading of bash can be shown complete;
  Claude Code's own analyzer, which this mirrors, is documented as no boundary.
- **A lexer of our own**, **an executor of our own** (in effect a small shell), and **argv-only matching**, which loses variables, pipes, and `$(…)`.
- **`mvdan.cc/sh`'s interpreter**: Go in a Python repository and a replacement for bash, which loses Claude's cwd tracking when it runs as a child;
  its handlers are documented as no isolation.
  **Its parser, through `shfmt --to-json`**: more faithful, but a Go binary from a third-party wheel and a subprocess per check.
  **bashlex**: GPL-3.0, which the Apache-2.0 framework cannot depend on.
- **Claude's own `Bash(...)` rules**, which the original proposal translated rosters into:
  under `--dangerously-skip-permissions` they cannot express an allowlist, and they would be a second matcher with semantics of their own.
- **Enforcement in Claude's shell prefix**: the prefix sees Claude's wrapper (snapshot sourcing, `eval`, cwd tracking) rather than the model's line.
- **`*` across arguments, or prefix-only entries**: no entry could hold a command to one argument.
- **A list of builtins argued safe one by one**: the holes sit in bash mechanisms (evaluated names, arithmetic, quoting inside `${…}`), so the matcher refuses the mechanisms, classifies every builtin, and checks both against bash by generation.
- **Unwrapping wrappers** (`timeout`, `nice`, `env`) the way Claude Code does:
  more surface for a convenience an entry gives.
