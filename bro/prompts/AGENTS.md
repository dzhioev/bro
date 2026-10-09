# Prompt store

Centralised prompt store.
Five loading conventions:
auto-inject into every bro + `ride solo|along` session (`shared/`);
serve as a reference doc
— injected into `ride solo|along` sessions or mounted as a `FileSource` tool (`*.md`);
inject the session fragments at launch (`hold.md` composing `holds/`, plus `member.md`, `summoner.md`, `summoned.md`, and `turn_end.md`);
splice into an opting-in text via `{{include}}` (`fragments/`);
load explicitly by name (top-level `*.prompt` / `*.prompt.template`).

## Files

- `*.prompt` — plain text, used as-is.
  Loaded with `bro.prompts.get_prompt('name.prompt')`
- `*.prompt.template` — Python `str.format` template.
  Requires kwargs at load time;
  `get_prompt` enforces "template ↔ kwargs" symmetry
  — passing kwargs to a non-template, or omitting kwargs for a template, raises

Prompt content may carry `bro.base.template` directives over the surface facts (`#creds`, the harness's own facts, `#hold`, and session-fragment `#talk`; grammar and semantics: `bro/reference/template.md`):
every rendering surface renders its text once with its registered `Harness` object via `bro.mcp.render_text`
— `BaseBro.composed_prompt(harness, hold=…)` for an explicitly selected harness, `ride/ride/claude/system_prompt.py:session_append_prompt` for managed Claude sessions
— so a directive works in `shared/` and bro class prompts alike.
`FileSource`-served docs are the exception:
one rendering is read by every harness, so their bodies must be surface-neutral
— `FileSource.read` supplies no facts and a surface directive raises.

A prompt describes harness-specific behavior through a capability fact or passage that each harness supplies from `Harness.facts()`.
Use a condition on the capability when the prompt owns the wording, and `{{insert #name}}` when the harness owns it.
The open `#harness` fact remains the escape hatch for a distinction no declared fact covers, not the first way to fork a prompt.
A condition on a tool the harness offers is no harness fork and stays prose:
the model reads its own tool list, and one harness offers different tools by mode.

`PromptLoader` is the contained directory-backed loader;
`__init__.py` binds the framework's module-level `get_prompt` / `get_prompt_path` surface to `bro/prompts/`.
Consumer packages bind their own loader for package-local prompts.
Do not `open()` prompts ad-hoc from elsewhere.
Names are contained to the loader's directory:
a name that resolves outside it (`..` traversal, absolute path) raises.
`get_prompt_path(name)` returns the `Path` if a caller needs to hand the file off to something that wants a path rather than the body (e.g. `bro.datasources.file.FileSource`).
`{{include <name>}}` directives resolve through it too
— `bro.mcp.render_text` wires `get_prompt` as the template engine's include resolver, so a spliced prompt loads exactly like a directly-requested one.

## Auto-injected `shared/` directory

`bro/prompts/shared/*.md` is appended to the system prompt of every Bro (see `bro/bro.py:_load_shared_prompts`) AND injected into every `ride solo|along` Claude Code session via `ride/ride/claude/system_prompt.py:_load_base_prompts`.
Conventions that must hold across both surfaces and at every hold (e.g. word choices) belong here.
Files are sorted alphabetically at load time, so prefix with `00-`, `10-`, etc. if order matters.

## `*.md` reference docs

A markdown reference doc in `bro/prompts/` (loader names are `/`-relative) reaches sessions one of two ways:

- **injected** — listed in `ride/ride/claude/system_prompt.py:_BASE_PROMPT_FILES`, appended to every `ride solo|along` session's `--append-system-prompt`.
  For content every managed Claude session must carry unconditionally.
- **tool-served** — declared as a `FileSource` in `bro/datasources/references.py`, then declared in a bro's `tools` either on its own as `source(<doc>)` (a `read` tool of its own namespace) or as `man('<topic>')`, joining that bro's manual:
  on every harness the bro serves, and the framework lists whichever it mounts in the bro system prompt's `## Data sources` block.
  For reference docs the agent consults on demand;
  the body must be surface-neutral (no `#harness` forks — `FileSource.read` renders with no facts and raises on one).

Current reference docs:

- `environment.md` — session-banner playbook:
  every surface calls the `bro::banner` service tool and reads this doc through the `environment` page (`ride banner --llm` stays as the human CLI).
  Tool-served only — not injected

- `tool_names.md` — inserts the selected harness's `tool_name_rule` passage;
  one file serves every surface.
  Managed Claude sessions get the Claude harness's `ns::tool` → `mcp__ns__tool` rule, injected here;
  bro-native LLM runs compose the bro harness's `ns::tool` → `ns__tool` rule through `BaseBro.composed_prompt`.
  Deliberately no `FileSource`

## Session fragments

`bro.prompts.session_fragment(hold, …facts)` renders the text a launch surface appends after the composed prompt, and every injection site calls it
(`ride/ride/claude/system_prompt.py:session_append_prompt`, `bro/bro.py:BaseBro.system_prompt_for`).
Those callers pass `bro.summon.talk()` as the `#talk` fact for the summoned contract;
unset stays unpublished and empty means the run's quest is mute.
It is the joined-party warning when the run is a member, the summoner’s watch when the run may summon, the summoned-delivery contract when another session is waiting on it, their one shared turn-end contract, then the hold fragment
— last, where instruction recency is strongest.

### Party-member contract

`member.md` states that a joined session shares its summoner’s tree and works beside it.
It renders when `bro.summon.party_member()` reads `RIDE_PARTY_MEMBER` from the launch environment.

### Summoner contract

`summoner.md` (top level) tells a session that may summon about the runtime-owned session watch, so every summon's lifecycle and chat remains observable without a model-started command.
It renders only for a run whose `launch.bro.bros` set (`bro.summon.effective_may_summon()`) is non-empty.
The same text states the notification trust rule and tells the summoner how to exchange questions, continue a retained quest, cancel a child, and wait by ending a turn.
The concrete-Bro family's `watch` spell separately starts a persona-admitted command through `bro::watch` and stops it through `bro::unwatch`.

### Summoned contract

`summoned.md` (top level) states what a summoned run owes its summoner and when to deliver it.
It renders only for a run `bro.summon.summoned()` reports as summoned, and its text does not branch on the hold
— the duty comes with being summoned, so an attended or guided child carries the same text a spawned unattended one does.
Its `#talk` branches admit only the quest's live moves:
a speaking summoner reaches the child through the runtime-owned session watch.
`worker.say` enables progress, `worker.question` asks once and receives the reply through the watch, and a child without that right raises instead of asking.

### One-shot turn-end contract

`turn_end.md` is the one shared end-of-turn contract appended when either the summoner or summoned contract applies.

### Hold text

A session's hold — its user-involvement level
— is one of `unattended | detached | attended | guided`, ordered from no human channel to human-driven.
Every session gets exactly one level's text, picked by the launching surface at session start
— a session is told its hold, never left to detect it at runtime:

- `ride solo|along` picks by its `--hold` flag for the managed Claude session append prompt (flag semantics: `bro/reference/ride.md`)
- the bro-native launch surfaces pick it through `bro/bro.py:BaseBro.system_prompt_for`
  — `run()` defaults unattended, `send()` guided, with every launcher's `--hold` overriding (per-surface defaults: `bro/launch/AGENTS.md`, "Display and holds")

`hold.md` (top level, not in `_BASE_PROMPT_FILES`) selects the per-level file in `holds/` via an exhaustive `{{iff #hold = …}}` chain and `{{include}}`s it;
the three non-guided level files share `holds/authorization.md`, the full-authorization block, and the three interactive levels
— detached, attended, guided
— share `fragments/interaction.md`, the interaction policy.
`bro.prompts.hold_fragment(hold, …facts)` is the one rendering path, composed by `session_fragment`.

The level files carry each level's session-wide conventions:
unattended the never-ask + `raise` convention, detached the carry-questions-into-the-report convention, attended the confirm-its-own-conclusions convention, guided the confirm-each-significant-step convention.
A passage of a persona prompt or a spell that applies at some levels only conditions on `#hold` where it stands (`bro/reference/conditions.md`, "Facts").

## Top-level one-shot prompts

`*.prompt` / `*.prompt.template` files at the top level are framework one-shot prompts loaded by name from their callers.

## Adding a prompt

- **One-shot**:
  drop `<name>.prompt` (or `<name>.prompt.template` for `str.format` slots) at the top level.
  Load with `get_prompt('<name>.prompt'[, **kwargs])`
- **Auto-injected into bros and `ride solo|along` sessions**:
  drop a `*.md` in `shared/`.
  Conventions that must hold for both surfaces at every hold (word choices, tone) belong here
- **Include fragment**:
  drop a `*.md` in `fragments/` and splice it with `{{include fragments/<name>.md}}` from each opting-in text.
  For a convention that applies only where a capability exists
  — e.g. `task_tracker.md`, included by every persona that mounts task-tracker tools rather than injected everywhere
  — or only at some holds, e.g. `interaction.md`, included by the interactive level files
- **Reference doc**:
  drop a `*.md` under this package, then either add its relative name to `ride/ride/claude/system_prompt.py:_BASE_PROMPT_FILES` (injected into every `ride solo|along` session) or declare a `FileSource` for it in `bro/datasources/references.py`
  — listed on a bro directly or as `man('<topic>')` (tool-served on demand, every harness — e.g. `environment.md`)
