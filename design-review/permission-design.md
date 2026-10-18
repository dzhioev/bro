# Permission design: design review

Design review only: this file is never merged.
The source of truth is the `## Design` section of [issue 978](https://github.com/dzhioev/bro/issues/978), where the settled design lands;
this copy and its branch are deleted when the review ends.
The model it adopts is `permission-model.md` beside it.

## Design

How the model on [`permissions: the use/give model`](https://github.com/dzhioev/bro/issues/985) is adopted in this repository.
Three amendments agreed with the task owner during this design are folded into that model:
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
Every module is tested against a brute-force evaluator over small finite domains, an unbounded base standing in as the values a case names plus fresh ones, with the model's examples as fixtures.

- `values.py` — the value-set families, each computing intersection, difference, emptiness, and membership:
  finite and cofinite sets for scalar options over strings (target, instances, harness, provider, model);
  every set for small finite domains (placement, hold, effort, flags);
  finite unions of integer intervals for whole numbers (timeouts in seconds, ports), an end left open meaning unbounded;
  and, for subset options, finite unions of lattice intervals `[Y, X]` (every S with Y ⊆ S ⊆ X).
  Subset options ship over finite base sets only, the talk rights and the registered credential kinds, where these intervals are closed under difference;
  over an unbounded base they aren't, so an option such as a webview origin list needs a richer family first (#987).
- `patterns.py` — descriptors, patterns, matching, the meet of patterns and of pattern sets, inclusion of pattern sets, and the prepare-time match, in which conditions on options a request leaves unset count as met.
- `authority.py` — pairs of use patterns and recipient patterns, entries, authorities (join) and caps (meet), per-atom evaluation, ⊤,
  and lazy entries: a finite family the reading fixes, which the host evaluates only for the atom and the start being checked (the configured-credentials entries under "Bases").
- `rules.py` — normalization against a type's declaration, the use rule, the gift rule, admission checks, joint-start prepare and commit, override checks, and refusals naming the rule or check and the atom or option.
- `syntax.py` — parsing and canonical rendering of the grammar under "Syntax", which every view renders through and which spells every value set a family computes.

### Type declarations

`WorkerType` in `bro/worker_types.py` declares options in place of `launch_schema` and the `LaunchSet`, `LaunchFlag`, and `LaunchPass` payloads:
per option its kind, domain, value-set family, whether it is open, and whether it is overridable;
`defaults(values, reading)`, `validate(descriptor)`, and `base(descriptor, reading)`;
and the mapping from its wire fields to option values and ungoverned content.
An open option's `*` sits in every bro base, so only a cap or the operator's authority narrows it.
The call shape keeps overridable options authority-neutral:
`base` and the defaults of fixed options receive only fixed options' values, and the defaults of overridable options are computed after them.
Type code makes no permission checks of its own: the launch control runs the rules, and the type runs the start.

### Shipped types

`bro`:

| Option | Kind and domain | Default | Open | Overridable |
|---|---|---|---|---|
| `target` | scalar, bro names `[a-z][a-z0-9-]*` | none; must be set | no | no |
| `placement` | scalar: `boxed`, `unboxed`, `join` | `boxed` | no | no |
| `harness` | scalar, harness names | the reading's summon harness | yes | no |
| `provider`, `model`, `effort`, `fast` | scalars, `fast` a flag | the first recipe in the reading's `bros.<target>.llm` list that the start's harness serves, `default` meaning the harness's own, under the fields the request sets as `--llm` fills them | yes | no |
| `hold` | scalar, the hold levels | `unattended` | yes | no |
| `talk` | subset of the four talk rights | `[worker.say]` | yes | no |
| `timeout` | scalar, whole seconds or `none` | 1800 | yes | no |
| `cred.<kind>` | scalar over instance names, one per registered kind | the configured pick of each kind the start holds | no | yes |
| `add`, `drop` | subsets of the registered credential kinds | `[]` | yes | yes |

These defaults are a summon's.
A default may not depend on who starts the mission, so the command line of a root start and of a joint start's commit sets `harness`, `hold`, `timeout`, and `placement` as the operator's values,
from its flags, `[tool.bro]`, and the mode's own defaults.
The wire mapping adds `worker.say` to a requested `talk`.

A bro start holds the kinds its configuration holds
— the needs of every component the target could mount under the start's harness and provider, the recording kind, and the reading's credential grants and revokes for that target
— plus `add`, minus `drop`.
The components it could mount are the union over every outcome of its `#creds` gates, each outcome deciding which of the gates' kinds are present,
since an `iff` alternative or an inherited entry that a closed gate uncovers can need what the reach with every gate open doesn't.
The harness's own needs count among them: the claude harness's auth kind, and under the bro harness the LLM key of the recipe's provider.
So the defaults don't depend on which instances exist, and a starter must be allowed every held kind's value, one that only an unselected component needs included.
When the start runs, the gates resolve against the instances present for the descriptor's picks, and only the components they select count:
their required kinds must be present, their optional ones are hydrated when present, and a kind that only unselected components need is not hydrated at all.
A held kind defaults to its pick: the host's `creds` layers, then the store's `defaults`, then the empty instance.
Setting `cred.<kind>` holds that kind at that instance.
A descriptor is valid when `target` is set, no kind is both set and in `drop`, the harness can run the recipe, and `join` isn't a joint start's placement.
`prompt`, `into`, `share`, `step_id`, and `index` are ungoverned content.
Existence is checked when the start runs: the target and harness installed, every held required instance present (an optional kind no layer picked is skipped when absent), `into` resolvable.
An uninstalled target normalizes with no declared needs or recipe, so it fails only at that check.

`webview`: `vnc`, a flag, guarded;
`vnc_port`, 1024–65535 or `auto`, default `auto`, open, a port needing `vnc`, where `auto` spells today's omitted port;
`cred.cookies`, instance names, guarded, unset meaning no profile;
`timeout`, whole seconds or `none`, default `none`, open.
None is overridable, and the base is empty: a webview starts nothing.
`allowed_origins` and `blocked_origins` are ungoverned content:
Playwright's filter lets top-level redirects through, so a permission over them would promise a limit the browser doesn't keep, and governing them waits on #987.

`benchmark`: `timeout`, whole seconds, default 12 hours, open.
The workspace-relative `config` is ungoverned content, and the base is empty.

### Readings

A root's launch takes the reading its tree carries, once, in the bundle process it re-executes into:

- the parsed `~/.bro.json`, whole: its layers, `llm` presets, `bros.<bro>.llm` lists, `summon-depth`, and `operator`;
- the store's `creds.json` `defaults`;
- the attachment's identities, which project entries match on, and its `[tool.bro]`:
  a path attachment's working-tree `pyproject.toml` as it stands at the launch, a URL attachment's at the commit the launch resolved;
- the installation the root runs, with its bros, worker types, credential kinds, harnesses, and providers, whose runtime bundle is frozen for the root's lifetime anyway.

The broker root holds it in memory, and every start in the tree, a manual child's commit included, normalizes and computes bases under it.
No source is re-read mid-ride:
a configuration edit reaches a tree through a resume, and a malformed edit no longer breaks a running ride's summons.
The host expands a summon's `llm` preset under the reading, so an MCP summon may name one, and the in-session `summon` CLI passes `llm` through.
Which instances exist, their material, and `creds.json` `sources` are not part of a reading and stay live.

### Bases

A bro mission's base folds, in order:

1. Universal entries: `*` for every open option of every type;
   the start atom `bro` and `bro.placement=boxed`, the framework's seeds;
   all at ⊤.
   The configured-credentials entries come with them:
   `bro.cred=<kind>+<instance>` for each kind and instance, usable in the starts whose target, harness, and provider make the reading's configuration hold that kind at that pick, `add` and `drop` aside,
   and giveable to none.
   They are finite, since the reading names finitely many targets and the installation finitely many harnesses and providers, and the host evaluates them only for the start being checked.
   Starting a bro thereby permits that bro's own configured credentials for that bro only, and a cap can still cut them.
2. The target persona's `may_summon` (one `@<bro>` each) and `may_launch` (one type each), at ⊤.
3. The configuration layers for the target: `[tool.bro]`, the host's `defaults`, the project URL entry, the project path entry, then the URL and path `bros.<target>` entries.
   Each layer applies its caps (`revoke`, `limit`) and then its `grant`, so a later layer wins;
   one layer naming a permission in both `grant` and `revoke` fails.
   A layer naming a type, option, or kind the installation doesn't know fails the launches it applies to, so one shared file can carry another installation's names in entries this one never applies.

A plain grant is ⊤ — use, and give to anyone — which is how holding a name lets a mission grant it on today.
`[tool.bro]` refuses entries and picks naming a credential instance, since a repository doesn't choose host material.
The operator's authority O is ⊤ under the caps in the host config's new `operator` entry (`revoke`, `limit`), and every operator step — a root start, a commit, an override, a resume — is checked against it.

### Starts

A root start (`ride along|solo`) is the operator's request:
the flags' option values (`--unboxed`, `--hold`, the LLM flags, `--harness`, `--grant|--revoke <kind>`, `--cred`), its gift (`--grant`), and its cap (`--revoke`, `--limit`).
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
- Commit: `ride along|solo --summoned <token>` re-executes into the root's runtime, as today, and sends the operator's values (placement from `--boxed|--unboxed`, hold, recipe, harness, and the credential flags) over the operator channel.
  The host fills the remaining defaults under the root's reading, checks validity,
  runs the use rule
  — the starter's values and the defaults against the starter's authority as it stands, the operator's values against O, all matched against the final descriptor
  — and the gift rule on that descriptor, and returns it.
  The descriptor holds a value for every held kind, the harness's auth kind and LLM key included, so the launcher claims the token and hydrates exactly the credential values it names;
  it no longer reads `~/.bro.json` itself.
- The CLI exposes less than the model allows: the bro positional must match the target, and `talk`, the prompt, `into`, `--env`, and authority names are refused, the last set by `ride override` once the child runs.
  A manual summon needs no placement right of its own, since the operator chooses placement.

### Overrides

`ride override <name> [--grant N]… [--revoke N]… [--cred K+I]…` names a workspace or a joined member, and takes `@<bro>` names and credential kinds only, as the Goal limits it.
Its checks are the model's Override paragraph.
It re-executes into the target root's runtime, which the root's control file names;
a root launched before the switch has no control file and is refused by name.

- `--grant @x` puts `@x` in Γₒ and removes any Kₒ entry for it, `--revoke @x` the reverse, and the latest flag for a name wins, as resume flags do today.
  The session may summon x from its next request, and a child already running keeps what it has.
- A session that could summon nothing at start rendered its prompt without the summoner and turn-end fragments.
  It renders them into its session directory at start instead, and the session watch delivers that text once, beside the notice of the first override that gives the session a bro target.
  `quest_watch` stays mounted only where the session could summon at start, and `summoner.md`'s sentence naming it renders only where the tool is mounted.
- `--grant k`, `--revoke k`, and `--cred k+i` change `add`, `drop`, and `cred.k`.
  The host normalizes again under the mission's reading, checks the changed values against O and their instances' existence, and the session's supervisor applies them:
  the root for the sessions it spawned, through the container it started, the private root it created, or a joined member's own store and install directory;
  the manual launcher for its child, through an operator-channel connection it keeps.
- Applying touches only the kinds the override changes, at the stable paths the session already uses, so work on every other kind runs on undisturbed.
  The applier stages the change and runs an in-session `credentials apply` step, which publishes it in an order that keeps every read consistent:
  new instance files first, unreferenced;
  then the store's index, `creds.json`, replaced by rename, carrying the new picks and the next generation number, which is the commit point;
  then each changed hook file and command wrapper, and the environment file, each replaced by rename;
  and last the explicit removal of what the override dropped or replaced, since `docker cp` merges and would leave a revoked instance readable.
  A command that already opened a file keeps what it read, one that opens it later reads the new material, and a dropped kind's paths stop resolving, which is the revocation.
  A store read that finds its instance file gone re-reads the index once when the generation moved, so a read racing an override returns the new pick or none.
  Every step after the commit point is idempotent and retried until done, and a failure before it leaves the session as it was.
- Every override carries an operation id, and the generation a credential override publishes records it.
  The host checks the override, marks it pending on the mission, and asks the applier to apply it;
  applying is idempotent per operation id, and its acknowledgement names the generation the session reached.
  On the acknowledgement the host writes the resume record, then commits its own record and the journal entry, and replies `applied`;
  a resume record it can't write keeps the operation pending and is retried, since a resume must not restore values the session no longer runs.
  Without an acknowledgement within its bound, the host replies `uncertain`, naming the operation,
  and reconciles it from the applier's report of the session's generation once the connection is back or at the next override of that mission:
  it commits if the session reached the operation's generation and drops the operation otherwise.
  While an operation is pending, further overrides of that mission are refused naming it, and the mission's own starts proceed, since a credential override never changes authority;
  an operation still pending when its mission ends is dropped, so a resume restores the values the record last committed.
  An `@bro` override needs no applier: the host writes the resume record and then commits, and refuses the override if the write fails.
- The environment file sits in the session's install directory and restates every hook variable, with unsets for the ones that go.
  Claude's shell prefix (`CLAUDE_CODE_SHELL_PREFIX`), which already wraps every Bash and hook command, sources it;
  the framework's process starters — watch producers, `cli()` tools, and the bro harness's jobs — apply it as an overlay, since the prefix doesn't reach them and brash can't source a script.
  The hooks' `bin` directory is on PATH from the start, even when empty, because Claude's shell snapshot pins PATH.
- The credential store checks the index's generation on each read and re-reads the index when it moved.
  An in-process consumer that derives state from credential material keys it to that generation and re-derives it at its next use:
  web search's lazily latched key and brog's backend and repository move onto it, and the credential-override stage audits every other consumer the same way.
  A toolset that owns resources, such as webview's open browsers, keeps them.
- Unchanged mid-run: tools and features a credential would switch on, since the tool roster and the prompt render at start;
  what a process latches at start and can't re-derive, such as Claude's token, the bro harness's LLM client, and the recorder's trails client;
  and material the session already read.
  The override's reply names, for each kind it changes, what reaches the session only at its next start.
- The host journals each override on the mission as a transition its worker end can see, where today a worker sees only its mission's chat, and the session watch wakes the session with what changed.
  Every session with a broker channel arms its session watch, and the turn-end check no longer depends on start-time targets.
  The record keeps the override, and a resume carries it.
- The operator channel is one broker channel the root provisions for the operator, its token and runtime in a 0600 file under `<runtime-root>/control/`, which no container mounts.
  Its requests — `override`, `commit`, and a listing of the tree's missions by name — are the operator's, and no mission's channel can send them.
  Like unboxed scoping, it is no boundary against an unboxed session, which runs as the operator's own user.
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
- The resume record keeps the launch shape it keeps today and replaces `cred`, `grant`, `revoke`, `llm`, and `resolved_llm` with the set values, gift, cap, and operator layer.
  It drops `summon_depth` and `summon_harness`, which a resume takes from its new reading where today it keeps the first launch's,
  and a manual child's names no runtime bundle, so a resumed one freezes the invoking installation like any other root.
  An override rewrites it.
- The launch audit adds the descriptor, the gift and cap, and a refusal's rule and atom.

### Syntax

One string grammar in every source and on every surface:

- A permission is `type`, `type.option=values`, `type.flag`, `@<bro>` for `bro.target=<bro>`, or `@*` for `bro.target=*`.
  Credential options are spelled by credential name: `bro.cred=github+{dev,reviewer}`, `webview.cred=cookies+*`, and `bro.cred=github+` for the empty instance.
- Values are `v`, `{a,b}`, `*`, `!S`, and integer ranges such as `1..3600` or `60..`;
  a subset option takes `[a,b]`, `<=[a,b]` for every subset of, and `>=[a]` for every superset of;
  `|` and `&` combine value sets, so every set a family computes has a canonical spelling, such as `>=[github]&<=[github,aws]` for a lattice interval.
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
browser, whose `may_launch` gives it `webview`, runs `webview open --cookies assistant`.
assistant holds no `webview`, so the use rule refuses its own `webview open` at the start atom.

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
| a child's configured credentials | defaults, permitted by the configured-credentials entries |
| launch-flag `--grant` and `--revoke` of names | the root's gift and cap |
| launch-flag `--grant` and `--revoke` of kinds, `--cred` | the root's `add`, `drop`, `cred.k` |
| a summon's `grant` and `revoke` | the child's gift, forwarded, and cap |
| a join | `placement=join`, which give-only recipients exclude by default |
| an unmarked summon starting boxed if held, else unboxed | the fixed default `boxed` |
| a manual summon | a joint start: prepare by the summoner, commit by the operator |
| `ride resume` flags | the operator layer and credential values, with defaults applied again |
| `RIDE_LAUNCH` | the host's `authority` request; the variable goes |
| a webview's origin lists | ungoverned content |
| the benchmark trial's `--revoke :launch.bro.party.boxed --grant :launch.bro.party.join` | `--revoke bro.placement=boxed --grant bro.placement=join` |

### Visibility and refusals

- A mission reads its descriptor and authority through a new `authority` request on its own channel.
  Every reader of today's `RIDE_LAUNCH` reads it instead.
  The prompt's `#may_summon`, Claude's appended system prompt, the summoner and turn-end fragments, and `quest_watch` read it once at start, each process caching that read;
  spell casts and the banner read it live.
  The banner shows `may_summon` (bro targets it may use, a conditional one shown with its `for`), `permits` (its other usable entries), and a new `may_give` (give-only entries with their `to`), in the canonical syntax.
  A process with no channel, such as a `ride exec` shell, a `BROKER_DISABLED` root, or `bro show`, renders no authority rows.
- `ride scope` prints the prospective descriptor with its defaults, the base authority, each credential value's state (`PRESENT`, `SKIPPED`, `MISSING`), and every instance the authority names as `PRESENT` or `MISSING`;
  a missing instance fails nothing at launch.
- A refusal names its rule or check and the atom or option, such as `use rule: bro.cred=github+reviewer is usable only in starts with target bro-eyebro`.

### Adoption notes, addressed

- Joins are no boundary: give-only recipients exclude `placement=join` unless a pattern names placement.
- Holding a name means granting it: plain grants are ⊤ in bases, and a plain `--grant` on a summon forwards.
- `pass` per credential kind: `cred.<kind>` on `bro`, `cred.cookies` on `webview`.
- Defaults: `placement` defaults to `boxed`;
  the LLM key and every credential pick are `bro` defaults, and the configured-credentials entries let a child's own configured credentials pass for that child only.
- Readings: one per root, taken at its launch, never re-read mid-ride.
- Unrestricted fields: every open option's `*` in every bro base;
  the prompt, `into`, `share`, provenance, the webview origin lists, and the benchmark config are ungoverned content.
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
- `benchmark/bro/benchmark/harbor_agent.py`: the trial's command line in the new grammar, with a trial-bundle format bump so a cached pre-switch bundle isn't reused.
- Also touched:
  `ride/ride/peer_facts.py` (records),
  `ride/ride/session.py` (the resume record, root start, resume, commit),
  `ride/ride/cli.py` (`ride override`, `--limit`, commit),
  `ride/ride/broker_root.py` (the reading, the operator channel),
  `ride/ride/root.py` (`RIDE_LAUNCH` goes),
  `ride/ride/scope_report.py` (`ride scope`),
  `ride/ride/pending_launch.py` (the prepared request),
  `ride/ride/do_ride.py`, `ride/ride/claude/shell_prefix.py`, `bro/watches.py`, `bro/llm/cli_tool.py`, and the native runner and jobs (the environment file and its overlay),
  `ride/ride/workspace/docker.py` (the applier's copy into a running container),
  `bro/base/credentials.py` (the staged apply, the index's generation, and the store's refresh),
  `bro/datasources/web_search.py` and `bro/brog/system.py` (caches keyed to the generation),
  `bro/base/host_config.py` and `bro/workspace/project.py` (`limit`, `operator`),
  `bro/bro.py` and `bro/prompts/__init__.py` (held kinds with gates open, `#may_summon`, `quest_watch`, and the deferred contract),
  `ride/ride/claude/system_prompt.py` and `bro/turn_end.py` (the authority read),
  `bro/summon.py` and the summon tool, `bro/workspace/banner.py`, and webview's `cli.py` and `mcp.py` (`cred.cookies`).

### Documentation

- `bro/reference/permission_model.md`: the model, from the model task.
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
  `extending.md`, `conditions.md`, `template.md`, `bro/setup/AGENTS.md`, `bro/prompts/environment.md`, `README.md`, `benchmark/README.md`, the `ask` and `reflect` spells,
  the `lead` persona's prompt and its `orchestrate` spell, the `browser` persona's prompt, and the touched subsystems' `AGENTS.md` files follow.

### Rollout

- No migration: resume records written before the switch are refused as unreadable, and those workspaces get dropped;
  `:launch.…` names fail as malformed.
- `~/.bro.json` can't serve both grammars.
  Every installation sharing it parses the whole file strictly on every read, and a running ride re-reads it at each summon,
  so a new-grammar entry anywhere fails every pre-switch reader, and a `:launch.` name every post-switch one.
  The switch is therefore one window: every pre-switch ride ends, every installation reading the file moves, and the file is rewritten.
  Here those installations are this checkout's `.venv` and dzhioev/ppp's pinned framework, whose bump is a follow-up;
  until it lands, ppp can't launch, and a pre-switch ride left running fails its next summon.
- A running ride keeps its frozen runtime and today's rules until it ends, and every child it starts, manual ones included, runs that runtime:
  no session of a pre-switch tree can run the new code, so verification runs in roots launched from the updated installation.
- The other cross-version contracts:
  - `[tool.bro]`: this repository's names no permissions;
    a consumer rewrites its own in its bump, and its pre-switch rides attached to the checkout fail their next summon once the bump is pulled.
  - Resume records: refused as above, except that a pre-switch root's manual child resumes into that root's bundle while the bundle exists.
  - Manual tokens: a token a pre-switch root minted re-executes into that root's runtime, as today.
  - The broker wire: one runtime serves each tree, and `ride override` re-executes into its root's runtime and refuses a pre-switch root by name.
  - The benchmark adapter and its trial bundle move together, through the trial-bundle format bump.
  - The webview image and the deployed trails server read no permission data and need no step.
- Order: (1) the algebra and the syntax;
  (2) type declarations, readings, and bases, tested but not yet enforced;
  (3) the operator channel;
  (4) the switch, as one change: enforcement, records, the syntax on every surface, the joint-start commit, the views, and the benchmark adapter;
  (5) `ride override` for `@<bro>` names, with the deferred contract;
  (6) credential overrides: the staged apply and its commit protocol, the environment file and its overlay, the store's refresh and the caches keyed to it, and both appliers;
  (7) the reference pages and the cheat sheet;
  then end-to-end verification.
- Verification: the algebra against the brute-force evaluator;
  every surface's refusals;
  end to end on the webview route, a test persona standing in for assistant gives browser its cookies profile, browser opens a webview with it, and the stand-in's own `webview open` is refused;
  a mid-run `@<bro>` grant followed by a summon, in a session that started with no target and receives the deferred contract;
  a mid-run `--cred` reaching `credentials get`, `git` through the environment file, and an MCP tool through the store's refresh, while work on an unrelated kind runs through it;
  a lost acknowledgement reconciled from the session's generation;
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
- Rebuilding toolset state on a credential change: it would orphan open webviews, while a store that re-reads on a generation change reaches every reader.
- A fresh generation directory per override behind a pointer switch: retiring the old directory breaks commands that inherited its paths, even for kinds the override didn't touch.
- Failing or warning a launch on a missing instance: the model checks existence when a start runs, and a shared host config names instances some machines lack.
- A versioned host-config path, so that pre- and post-switch installations could share a host: the owner chose one window over keeping two files in step.
- Governing the webview origin lists now: Playwright's filter lets top-level redirects through, and an empty `allow` meaning "any origin" runs against the order a cap relies on (#987).
- The summoner and turn-end fragments and `quest_watch` in every brokered session: about 2.1k characters of prompt in sessions that never summon, where delivering the contract with the first grant costs nothing until then.
- Mounting `quest_watch` mid-run: ride's MCP server is stateless and can't push a tool-list change, and a changed tool list re-reads the whole conversation uncached.

### Risks and edge cases

- The algebra is the security boundary, so its tests compare it with a brute-force evaluator.
- Behavior changes: an unmarked summon is `boxed` even where only `unboxed` is granted, and the refusal names `--unboxed`;
  a manual summon needs no placement right;
  `--cred` alone holds its kind;
  a summon may `--grant` or `--revoke` a credential kind, checked like a pass;
  `@*` lets a holder summon any bro;
  a configuration edit no longer reaches a running ride;
  timeouts are whole seconds;
  the in-session `summon` CLI no longer expands presets itself.
- A cap on a child's configured credentials refuses starting that bro unless the request drops the kind, and the refusal says so.
- An over-wide gift is refused, not trimmed;
  a gift of what the child's base already holds is harmless.
- A resumed child whose recorded gift names a type or option no longer installed is refused, naming it.
- A credential override reaches the commands started after it and every store read after it;
  its reply names what changes only at the next start.
- A contract delivered mid-run lives in the conversation, so a compaction can summarize it away;
  a resume renders it into the prompt again.

### Not in this design

- Renaming `WorkerType` to mission type throughout, to match the model's vocabulary: proposed, not needed.
- Mid-run changes of other authority names: they need no runtime support, so lifting the Goal's limit later is a change to the CLI alone.
- Governing the webview origin lists: #987.
- Still deferred: several operators, a starter changing its running child, hop-distinguishing recipient patterns, and tools a credential switches on mid-run.

### Related tasks

- #681, where a summon granting an `@bro` the child already carries is denied, is resolved: a gift of what the child's base already holds is harmless.
- #844's banner and `ride scope` framing is superseded by the views above.
- #845 is unaffected, since the grammar keeps bare credential kinds in `grant` and `revoke`.
