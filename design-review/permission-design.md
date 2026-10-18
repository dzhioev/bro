# Permission design: design review

Design review only: this file is never merged.
The source of truth is the `## Design` section of [issue 978](https://github.com/dzhioev/bro/issues/978), where the settled design lands;
this copy and its branch are deleted when the review ends.
The model it adopts is `permission-model.md` beside it.

## Design

How the model above is adopted in this repository.
Three amendments agreed with the task owner during this design are folded into `## Model`:
use carries patterns like give, an override's checks are spelled out, and a resume carries the values its principals set rather than the whole descriptor.

### Shape

- The host is the root ride's broker process.
  It takes one reading when the root launches, keeps a record of every mission in the tree, checks every start, joint-start commit, and override, and serves an operator channel.
  A resume starts a new ride and takes a new reading.
- One mission type per worker type: `bro`, `webview`, `benchmark`.
  A bro's target is an option, so `target=*` is a real wildcard and recipient patterns match on it.
- The algebra is pure core code in `bro/authority/`;
  the host side lives in `ride/` and in each worker type.

### The algebra (`bro/authority/`)

Consumer-neutral and free of I/O.
Every module is tested against a brute-force evaluator over small finite domains, with the model's examples as fixtures.

- `values.py` — the value-set families: finite and cofinite sets for scalar options over strings (target, instances, harness, provider, model);
  every set for small finite domains (placement, hold, effort, flags);
  finite unions of closed intervals for numbers (timeout, ports);
  and, for subset options, finite unions of lattice intervals `[Y, X]` (every S with Y ⊆ S ⊆ X, X finite or cofinite).
  Each family computes intersection, difference, emptiness, and membership.
- `patterns.py` — descriptors, patterns, matching, the meet of patterns and of pattern sets, inclusion of pattern sets, and the prepare-time match, in which conditions on options a request leaves unset count as met.
- `authority.py` — pairs of use patterns and recipient patterns, entries, authorities (join) and caps (meet), per-atom evaluation, ⊤,
  and derived entries, whose atoms and use patterns the host computes on demand for the descriptor being checked (the configured-credentials entry under "Bases").
- `rules.py` — normalization against a type's declaration, the use rule, the gift rule, admission checks, joint-start prepare and commit, override checks, and refusals naming the rule or check and the atom or option.
- `syntax.py` — parsing and canonical rendering of the grammar under "Syntax", which every view renders through.

### Type declarations

`WorkerType` in `bro/worker_types.py` declares options in place of `launch_schema` and the `LaunchSet`, `LaunchFlag`, and `LaunchPass` payloads:
per option its kind, domain, value-set family, whether it is open, and whether it is overridable;
`defaults(values, reading)`, `validate(descriptor)`, and `base(descriptor, reading)`;
and the mapping from its wire fields to option values and ungoverned content.
An open option's `*` sits in every bro base, so only a cap or the operator's authority narrows it.
A type is malformed if a fixed option takes a default from an overridable one or its base reads an overridable one.
Type code makes no permission checks of its own: the launch control runs the rules, and the type runs the start.

### Shipped types

`bro`:

| Option | Kind and domain | Default | Open | Overridable |
|---|---|---|---|---|
| `target` | scalar, bro names `[a-z][a-z0-9-]*` | none; must be set | no | no |
| `placement` | scalar: `boxed`, `unboxed`, `join` | `boxed` | no | no |
| `harness` | scalar, harness names | the reading's summon harness | yes | no |
| `provider`, `model`, `effort`, `fast` | scalars, `fast` a flag | the host's per-bro entry over the target's declared recipe, within the harness | yes | no |
| `hold` | scalar, the hold levels | `unattended`; a manual child's launch sets it | yes | no |
| `talk` | subset of the four talk rights | `[worker.say]`, which a request widens | yes | no |
| `timeout` | scalar, seconds or `none` | 1800; a manual child's commit sets `none` | yes | no |
| `cred.<kind>` | scalar over instance names, one per registered kind | the configured pick of each kind the start holds | no | yes |
| `add`, `drop` | subsets of credential kinds | `[]` | yes | yes |

A bro start holds the kinds its configuration holds — the target's needs under the start's harness and provider, the recording kind, and the reading's credential grants and revokes for that target — plus `add`, minus `drop`.
A held kind defaults to its pick: the host's `creds` layers, then the store's `defaults`, then the empty instance.
Setting `cred.<kind>` holds that kind at that instance.
A descriptor is valid when `target` is set, no kind is both set and in `drop`, the harness can run the recipe, and `join` isn't a joint start's placement.
`prompt`, `into`, `share`, `step_id`, and `index` are ungoverned content.
Existence is checked when the start runs: the target and harness installed, every held required instance present (an optional kind no layer picked is skipped when absent), `into` resolvable.
An uninstalled target normalizes with no declared needs or recipe, so it fails only at that check.

`webview`: `vnc`, a flag, guarded;
`vnc_port`, 1024–65535 or `auto`, default `auto`, open, a port needing `vnc`;
`cred.cookies`, instance names, guarded, unset meaning no profile;
`allow` and `block`, subsets of origins, default `[]`, open, where an empty `allow` means no allow-list;
`timeout`, default `none`, open.
None is overridable, and the base is empty: a webview starts nothing.

`benchmark`: `timeout`, default 12 hours, open.
The workspace-relative `config` is ungoverned content, and the base is empty.

### Readings

A root's launch takes the reading its tree carries:
the parsed `~/.bro.json` (layers, `llm` presets, `summon-depth`, `operator`),
the store's `creds.json` `defaults`,
the attachment's `[tool.bro]` at the root's base (layers, `summon-depth`, `summon-harness`, `[tool.bro.llm]`),
and the installation the root runs, whose runtime bundle is frozen for the root's lifetime anyway.
The broker root holds it in memory, and every start in the tree, a manual child's commit included, normalizes and computes bases under it.
No source is re-read mid-ride, so a configuration edit reaches a tree through a resume.
Credential material and which instances exist are not part of a reading.

### Bases

A bro mission's base folds, in order:

1. Universal entries: `*` for every open option of every type;
   the start atom `bro` and `bro.placement=boxed`, the framework's seeds;
   all at ⊤.
   And the derived configured-credentials entry: for a start d, `bro.cred=<kind>+<pick>` for each kind d's configuration holds, `add` and `drop` aside, usable in starts matching d's target, harness, and provider and giveable to none.
   Starting a bro thereby permits that bro's own configured credentials for that bro only, and a cap can still cut them.
2. The target persona's `may_summon` (one `@<bro>` each) and `may_launch` (one type each), at ⊤.
3. The configuration layers for the target: `[tool.bro]`, the host's `defaults`, the project URL entry, the project path entry, then the URL and path `bros.<target>` entries.
   Each layer applies its caps (`revoke`, `limit`) and then its `grant`, so a later layer wins;
   one layer naming a permission in both `grant` and `revoke` fails.

A plain grant is ⊤ — use, and give to anyone — which is how holding a name lets a mission grant it on today.
`[tool.bro]` refuses entries and picks naming a credential instance, since a repository doesn't choose host material.
The operator's authority O is ⊤ under the caps in the host config's `operator` entry (`revoke`, `limit`), and every operator step — a root start, a commit, an override, a resume — is checked against it.

### Starts

A root start (`ride along|solo`) is the operator's request: the flags' option values (`--unboxed`, `--hold`, the LLM flags, `--harness`, `--grant|--revoke <kind>`, `--cred`), its gift (`--grant`), and its cap (`--revoke`, `--limit`).
The launcher takes the reading, normalizes, checks against O, computes Hₘ = (base ⊔ Γ) ⊓ K, records the mission, and runs it.

A summon (`summon`, `bro::summon`, `launch {type: bro, …}`) and every other launch go through the launch control:

1. Resolve the starter's record.
2. Map the wire fields to option values — `placement` replaces `party` and `isolation`, `--pass k+i` sets `cred.k`, `--grant k` adds to `add`, `--revoke k` to `drop` — and to the gift (`grant`), the cap (`revoke`, `limit`), and content.
3. Normalize under the root's reading.
4. Run the use rule against the starter's authority, then the gift rule.
5. Run the admission check, the depth cap.
6. Record the mission and compute its authority.
7. Check existence, then run the type's spawn, container, or job.

A plain `grant P` on a summon gives P with full use and the starter's own recipients for it;
a qualified one gives exactly its clauses, which the gift rule bounds.

### Joint starts

- Prepare: `summon --manual` carries the starter's values (target, talk, passes, `add`, `drop`), gift, and cap, and refuses placement, hold, recipe, harness, timeout, and `share`, which belong to the launch.
  The host refuses unknown options and values, runs the use rule on the start atom and the options set with the prepare-time match, checks the gift's give against the starter's own, records the request, and writes the pending record.
- Commit: `ride along|solo --summoned <token>` sends the operator's values (placement from `--boxed|--unboxed`, hold, recipe, harness, and the credential flags) over the operator channel.
  The host fills the remaining defaults under the root's reading, checks validity,
  runs the use rule
  — the starter's values and the defaults against the starter's authority as it stands, the operator's values against O, all matched against the final descriptor
  — and the gift rule on that descriptor, and returns it.
  The launcher then claims the token and hydrates exactly the credential values the descriptor names;
  it no longer reads `~/.bro.json` itself.
- The CLI exposes less than the model allows: the bro positional must match the target, and `talk`, the prompt, `into`, `--env`, and authority names are refused, the last set by `ride override` once the child runs.
  A manual summon needs no placement right of its own, since the operator chooses placement.

### Overrides

`ride override <name> [--grant N]… [--revoke N]… [--cred K+I]…` names a workspace or a joined member, and takes `@<bro>` names and credential kinds only, as the Goal limits it.
Its checks are the model's Override paragraph.

- `--grant @x` puts `@x` in Γₒ and removes any Kₒ entry for it, `--revoke @x` the reverse, and the latest flag for a name wins, as resume flags do today.
  The session may summon x from its next request, and a child already running keeps what it has.
- `--grant k`, `--revoke k`, and `--cred k+i` change `add`, `drop`, and `cred.k`.
  The host normalizes again under the mission's reading, checks the changed values against O and their instances' existence, and the session's supervisor applies them:
  the root for the sessions it spawned, the manual launcher for its child, through an operator-channel connection it keeps.
  Applying rewrites the session's store (boxed through `docker cp` into the running container, unboxed in its private store root),
  the install hooks' files and command wrappers, and the session's environment script,
  then bumps the store's generation marker.
  The override takes effect once applied;
  a failure leaves the record and the session as they were.
- The environment script sits in the session's install directory.
  `do-ride` writes it at start from the install hooks, which still export their variables into the process environment, and an override rewrites it with exports and unsets.
  Claude's shell prefix (`CLAUDE_CODE_SHELL_PREFIX`), which already wraps every Bash and hook command, sources it when present;
  the bro harness reads it for each command it starts.
  It puts the hooks' `bin` directory on PATH from the start, so no new Claude hook is added.
- The session's MCP serving process checks the store's generation before each call
  and, when it moved, drops its cached store and rebuilds each mounted server's state through the toolsets' `state` and `close` hooks,
  between calls, keeping its tools and its connection.
  The bro harness's in-process servers reload the same way.
- Unchanged mid-run: tools and features a credential would switch on, since the tool roster and the prompt render at start;
  values the harness or its tool server took from its own environment at start, such as Claude's token and the bro harness's LLM key;
  and material the session already read.
- The host journals each override on the mission, and the session watch wakes the session with what changed, so every session with a broker channel arms its session watch.
  The record keeps the override, and a resume carries it.
- The operator channel is one broker channel the root provisions for the operator, its token in a 0600 file under `<runtime-root>/control/`, which no container mounts.
  Its requests — `override`, `commit`, and a listing of the tree's missions by name — are the operator's, and no mission's channel can send them.
  An override needs its root alive;
  a detached manual child has no host.

### Resumes

`ride resume <workspace>` with `--grant`, `--revoke`, `--limit`, and `--cred` is a start by the operator under a new reading.
It carries the record's set values, all now the operator's, its gift and cap, and its operator layer;
the flags replace credential values and `add`/`drop` entries per kind and the operator layer per name.
Defaults are applied again, so credential picks and an unnamed recipe follow the current configuration, and every check of a start runs against O.
A resumed child is a root, and the missions it had started don't survive.

### Records

- The host's record of a mission: its type, set values with who set each, descriptor, gift, cap, operator gift and cap, and computed authority;
  the reading is the ride's.
- The resume record keeps the launch shape it keeps today and replaces `cred`, `grant`, `revoke`, `llm`, and `resolved_llm` with the set values, gift, cap, and operator layer;
  summon depth and summon harness come from the reading.
  An override rewrites it.
- The launch audit adds the descriptor, the gift and cap, and a refusal's rule and atom.

### Syntax

One string grammar in every source and on every surface:

- A permission is `type`, `type.option=values`, `type.flag`, `@<bro>` for `bro.target=<bro>`, or `@*` for `bro.target=*`.
  Credential options are spelled by credential name: `bro.cred=github+{dev,reviewer}`, `webview.cred=cookies+*`, and `bro.cred=github+` for the empty instance.
- Values are `v`, `{a,b}`, `*`, `!S`, and ranges such as `1..3600`;
  a subset option takes `[a,b]`, `<=[a,b]` for every subset of, and `>=[a]` for every superset of.
- A pattern is `type{conditions}`, a condition being `o=S` (set, to a value in S), `o?=S` (unset or in S), `!o` (unset), or `o` (set).
  `@browser` is `bro{target=browser}`, and `@*` is any bro.
- An entry is `P`, `P for U…`, `P to R…`, or `P for U… to R…`.
  A plain entry is full use and give;
  a qualified one allows exactly its clauses, with `for *` and `to *` meaning all.
  In a `to` clause, a bro pattern that doesn't name `placement` never reaches a joined mission.
- Lists: `grant` adds entries, and a bare credential kind in it holds the kind;
  `revoke` removes plain permissions, and a bare kind in it drops the kind;
  `limit` caps with qualified entries, keeping only what they name.
- The lists appear in the host config's `defaults`, project, and `bros` entries, in `[tool.bro]`, and in `operator`, which takes `revoke` and `limit` only.
  On the command line they are `--grant`, `--revoke`, and `--limit` on `ride along|solo|resume` and `summon`;
  on the summon tool, the `grant`, `revoke`, and `limit` lists beside `passes`.
- A bare word is a credential kind or a mission type, and the registries refuse a name claimed as both.
- The everyday spellings stay: `--grant` and `--revoke` of `@<bro>` and of a kind, `--cred`, `--pass`, and the config `grant`, `revoke`, and `creds` lists.

The motivating case: the host config gives assistant `"grant": ["@browser", "webview.cred=cookies+assistant to @browser"]`;
assistant runs `summon --grant webview.cred=cookies+assistant browser '…'`, which forwards use with assistant's recipients;
browser, whose `may_launch` gives it `webview`, runs `webview open --cookies assistant`. assistant holds no `webview`, so the use rule refuses its own `webview open` at the start atom.

### Today's mechanisms, mapped

| Today | Under the model |
|---|---|
| `may_summon = ('x',)` | base entry `@x` at ⊤ |
| `may_launch = ('webview',)` | base entry `webview` at ⊤ |
| the seeds `:launch.bro`, `:launch.bro.party.boxed` | universal `bro`, `bro.placement=boxed` |
| `:launch.bro.party.join`, `:launch.bro.party.unboxed` | `bro.placement=join`, `bro.placement=unboxed` |
| `:launch.webview`, `:launch.webview.vnc`, `:launch.benchmark` | `webview`, `webview.vnc`, `benchmark` |
| a pass right `:launch.<type>.pass.k+i` | `<type>.cred=k+i`, use of the option atom |
| `--pass k+i` | the child's `cred.k = i`, checked by the use rule |
| a child's configured credentials | defaults, permitted by the derived configured-credentials entry |
| launch-flag `--grant` and `--revoke` of names | the root's gift and cap |
| launch-flag `--grant` and `--revoke` of kinds, `--cred` | the root's `add`, `drop`, `cred.k` |
| a summon's `grant` and `revoke` | the child's gift, forwarded, and cap |
| a join | `placement=join`, which give-only recipients exclude by default |
| an unmarked summon starting boxed if held, else unboxed | the fixed default `boxed` |
| a manual summon | a joint start: prepare by the summoner, commit by the operator |
| `ride resume` flags | the operator layer and credential values, with defaults applied again |
| `RIDE_LAUNCH` | the host's `authority` request; the variable goes |

### Visibility and refusals

- A mission reads its descriptor and authority through a new `authority` request on its own channel.
  The start-time prompt facts, `#may_summon` among them, render from that read, and the banner reads it live:
  `may_summon` (bro targets it may use, a conditional one shown with its `for`), `permits` (its other usable entries), and a new `may_give` (give-only entries with their `to`), in the canonical syntax.
- `ride scope` prints the prospective descriptor with its defaults, the base authority, each credential value's state (`PRESENT`, `SKIPPED`, `MISSING`), and every instance the authority names as `PRESENT` or `MISSING`;
  a missing instance fails nothing at launch.
- A refusal names its rule or check and the atom or option, such as `use rule: bro.cred=github+reviewer is usable only in starts with target bro-eyebro`.

### Adoption notes, addressed

- Joins are no boundary: give-only recipients exclude `placement=join` unless a pattern names placement.
- Holding a name means granting it: plain grants are ⊤ in bases, and a plain `--grant` on a summon forwards.
- `pass` per credential kind: `cred.<kind>` on `bro`, `cred.cookies` on `webview`.
- Defaults: `placement` defaults to `boxed`;
  the LLM-key chain and every credential pick are `bro` defaults, and the derived entry lets a child's own configured credentials pass for that child only.
- Readings: one per root, taken at its launch, never re-read mid-ride.
- Unrestricted fields: every open option's `*` in every bro base;
  the prompt, `into`, `share`, provenance, and the benchmark config are ungoverned content.
- Existence at run time: authority may name absent instances, `ride scope` lists them, and only a start using one fails.
- Records grow: set values with who set them, gift, cap, and operator layer, rewritten on override.
- Joint-start commit: checked by the host under the root's reading;
  the CLI exposes placement, hold, recipe, harness, and credentials only.
- Overrides are new: `@<bro>` names and credentials only, with the runtime support above;
  every other option stays fixed at start.
- The operator's authority: the host config's `operator` entry, ⊤ by default, checked at every operator step.
- Deferred extensions: cross-option use constraints are adopted as use patterns;
  several operators, a starter changing its running child, and hop-distinguishing patterns stay deferred.

### What the code becomes

- `bro/base/scope.py` goes: its grammar becomes `bro/authority/syntax.py`, and its layers the host config's.
- `bro/worker_types.py`: option declarations replace `launch_schema` and the launch payloads, and `PeerDescription` carries the descriptor and authority.
- `ride/ride/scope.py`: the reading, credential defaults and hydration from descriptor values, and `ride scope`'s data;
  `effective_launch` and `launch_covers` go.
- `ride/ride/launch_control.py`: wire mapping, normalization and the rules through `bro.authority`, records, existence checks, and the `authority` and operator-channel requests.
- `ride/ride/bro_worker.py`: the `bro` type's options, defaults, validity, base, and runs, placement from its option, and a pending record holding the prepared request.
- `browser/bro/webview/worker.py` and `bench/bro/bench/job.py`: option declarations;
  their permission checks go.
- Also touched:
  `ride/ride/peer_facts.py` (records),
  `ride/ride/session.py` (the resume record, root start, resume, commit),
  `ride/ride/cli.py` (`ride override`, `--limit`, commit),
  `ride/ride/broker_root.py` (the reading, the operator channel),
  `ride/ride/do_ride.py` and `ride/ride/claude/shell_prefix.py` (the environment script),
  the native runner (the environment script, server reload),
  `bro/base/credentials.py` (the store generation),
  `bro/base/host_config.py` and `bro/workspace/project.py` (`limit`, `operator`),
  `bro/summon.py` and the summon tool, `bro/workspace/banner.py`, `bro/watches.py`, and webview's `cli.py` and `mcp.py` (`cred.cookies`).

### Documentation

- `bro/reference/permission_model.md`: the model, from `## Model`.
- `bro/reference/permissions.md`: the system built on it — the shipped types, readings, bases and configuration layers, the syntax, every surface, joint starts, overrides, resumes, records, and refusals.
- `bro/reference/permissions_cheatsheet.md`: common scenarios in plain words, each with its configuration or command — let a bro summon another, or stop it;
  give a session a credential or pick an instance;
  pass an instance to a child, and grant the right to;
  let a bro open webviews, with VNC, with a profile;
  give a right you can't use (assistant and browser);
  relay it through a helper;
  let children join or start unboxed;
  restrict a child;
  run benchmarks;
  change a running session;
  resume with changes;
  guard yourself with `operator`;
  see what a session may do;
  launch a manual child.
- All three ship in the wheel and are served as man topics.
  `ride.md` keeps the credential store, hydration, and install hooks and points to the new pages;
  `extending.md`, `conditions.md`, `template.md`, `bro/setup/AGENTS.md`, `bro/prompts/environment.md`, the `ask` and `reflect` spells, the `lead` prompt, and the touched subsystems' `AGENTS.md` files follow.

### Rollout

- No migration: resume records written before the switch are refused as unreadable, and those workspaces get dropped;
  `:launch.…` names fail as malformed.
- A running ride keeps its frozen runtime and today's rules until it ends, while launches and resumes after the install take the new ones;
  a manual token an old root minted still re-executes into that root's runtime.
- Installations sharing one `~/.bro.json` move together, since each refuses the other's names.
- Order: (1) the algebra;
  (2) type declarations, readings, and bases, tested but not yet enforced;
  (3) the switch, as one change: enforcement, records, the syntax and every surface, the operator channel and the joint-start commit, the views, the docs, and the cheat sheet;
  (4) `ride override` for `@<bro>` names;
  (5) credential overrides: the environment script, the store rewrite, the MCP reload, and the manual launcher's applier;
  (6) end-to-end verification.
- Verification: the algebra against the brute-force evaluator;
  every surface's refusals;
  end to end on the webview route, a test persona standing in for assistant gives browser its cookies profile, browser opens a webview with it, and the stand-in's own `webview open` is refused;
  a mid-run `@<bro>` grant followed by a summon;
  a mid-run `--cred` reaching `credentials get`, `git` through the environment script, and an MCP tool after its reload;
  a manual summon's commit;
  a resume picking up a changed pick.

### Alternatives rejected

- One mission type per installed bro: "any bro" becomes an enumeration of the installed bros rather than a wildcard, tied to one installation.
- One `bro` type with unconditional use of every configured instance: a summoner could pass any bro's configured instance to any bro it may summon.
- A target's configured credentials outside the use rule, or a `configured` sentinel value: the first reopens the rule on unset fallbacks, and a cap on one instance could be bypassed through the second.
- An `off` value per credential option instead of `add` and `drop`: `*` would include dropping, and `off` would need a reserved spelling.
- A resume carrying the whole descriptor: credential picks would freeze at the first start.
- JSON entries or punctuation such as `>` and `?` for qualified entries, and qualified `revoke` strings: the first read worse and need shell quoting, and the second reads as the opposite of what it does.
- Give-only rights reaching joined missions by default: a join is no boundary.
- Overrides through the session's own channel, and `RIDE_LAUNCH` as the session's copy of its authority: a mission could forge the first, and the second can't change mid-run.
- A SessionStart hook writing `CLAUDE_ENV_FILE`: the existing shell prefix already wraps every command, so no hook has to be combined with ride's own.
- Restarting the MCP server on a credential change: Claude would have to re-initialize its sessions.
- Failing or warning a launch on a missing instance: the model checks existence when a start runs, and a shared host config names instances some machines lack.

### Risks and edge cases

- The algebra is the security boundary, so its tests compare it with a brute-force evaluator.
- Behavior changes: an unmarked summon is `boxed` even where only `unboxed` is granted, and the refusal names `--unboxed`;
  a manual summon needs no placement right;
  `--cred` alone holds its kind;
  a summon may `--grant` or `--revoke` a credential kind, checked like a pass;
  `@*` lets a holder summon any bro.
- A cap on a child's configured credentials refuses starting that bro unless the request drops the kind, and the refusal says so.
- An over-wide gift is refused, not trimmed;
  a gift of what the child's base already holds is harmless.
- A resumed child whose recorded gift names a type or option no longer installed is refused, naming it.
- A credential override reaches only the commands started after it and the reloaded tool server;
  its reply names what changes only at the next start.

### Not in this design

- Renaming `WorkerType` to mission type throughout, to match the model's vocabulary: proposed, not needed.
- Mid-run changes of other authority names: they need no runtime support, so lifting the Goal's limit later is a change to the CLI alone.
- Still deferred: several operators, a starter changing its running child, hop-distinguishing recipient patterns, and tools a credential switches on mid-run.
