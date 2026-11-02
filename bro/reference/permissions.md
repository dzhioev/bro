# Permissions

This is the reference for the permission system implemented by `ride`.
For copyable everyday setups, start with the [cheat sheet](permissions_cheatsheet.md).
The [permission model](permission_model.md) defines the algebra and its rules independently of this implementation.
Credential storage, hydration, and install hooks are in [ride](ride.md#session-permissions-and-credentials).

## Missions, use, and give

The host is the root ride's broker process.
It keeps the authoritative record of each mission and checks every start, manual commit, and override.
The shipped mission types are `bro`, `webview`, and `benchmark`.
A descriptor names the type and its option values, including defaults.
Starting a type needs its start permission and permission for every value the descriptor sets.
An option permission alone never permits starting its type.

Each permission has two independent parts:

- **Use** says which starts may use it, optionally restricted by patterns over the start's descriptor.
- **Give** says which new missions may receive it, restricted by recipient patterns.

A mission can give a right it cannot use.
It can give use to an allowed recipient, but cannot give wider onward recipients than it has itself.
An over-wide gift is refused, not trimmed.
Giving a right the child's base already supplies is harmless, but still has to pass the gift rule.
A child computes its own base; it never inherits its parent's authority wholesale.
Only a cap takes rights away.

The effective authority is the base joined with the starter's gift, restricted by the starter's cap, then joined with the operator's gift and restricted by the operator's cap.
Thus an operator can grant something the starter capped, or remove anything.
Already-running children keep their own authority when their parent changes.

## String syntax

The same grammar appears in configuration, CLI flags, tool lists, and rendered authority views.
Shell-quote entries containing spaces, wildcards, braces, brackets, or operators.

| Form | Meaning |
|---|---|
| `bro`, `webview`, `benchmark` | Permission to start that mission type |
| `@browser`, `@*` | `bro.target=browser`, or any target, including names installed later |
| `bro.placement=join` | One scalar option value |
| `webview.vnc` | The flag's `on` value |
| `bro.cred=github+reviewer` | A bro's GitHub credential instance |
| `webview.cred=cookies+*` | Any cookies instance for a webview |
| `bro.cred=github+` | The empty GitHub instance |

A bare registered credential kind in `grant` or `revoke` holds or drops that kind in the new session.
It is not an authority entry for passing that kind onward.
Type and credential-kind names cannot collide.
Credential options are internally named `cred.<kind>`, but permission strings use `type.cred=kind+values`.

### Value sets

| Form | Meaning |
|---|---|
| `v`, `{a,b}`, `{}` | One scalar, several scalars, no scalars |
| `*`, `!S` | The whole fixed domain, or its complement of S |
| `1..3600`, `60..`, `..3600` | Inclusive integer ranges, with an optional open end |
| `[a,b]`, `[]` | One exact subset value, including the empty subset |
| `<=[a,b]`, `>=[a]` | Every subset of, or every superset of, the named members |
| `S\|T`, `S&T`, `(S)` | Union, intersection, grouping; `&` binds tighter than `|` |

Subset options use finite bases: talk rights or registered credential kinds.
Subset grants do not combine element by element: permission for `[a]` and `[b]` does not permit `[a,b]`.
Scalar families include finite/cofinite strings, whole-number interval unions, and closed disjoint sums of integers and string sentinels.
For example, `bro.timeout=1..3600|none` includes both seconds and the literal sentinel `none`; a range alone excludes the sentinel.
Names must satisfy their option's base grammar even when written as quoted strings.

### Patterns and qualified entries

A pattern is `type{conditions}`.
Comma-separated conditions are all required; space-separated patterns are alternatives.
Conditions are `option=S` (set and in S), `option?=S` (unset or in S), `!option` (unset), and `option` (set).
`@browser` abbreviates `bro{target=browser}`, and `@*` matches any bro.
A clause's `*` matches all relevant types for give, or the permission's type for use.

| Entry | Use | Give |
|---|---|---|
| `P` | All starts of P's type | Any recipient |
| `P for U` | Starts matching U | None |
| `P to R` | None | Recipients matching R |
| `P for U to R` | Starts matching U | Recipients matching R |

A qualified entry supplies exactly its written clauses.
`for *` supplies unrestricted use; `to *` supplies unrestricted recipients.
A bro recipient pattern without a `placement` condition excludes joined missions.
This restriction applies to bro patterns such as `to @browser` and `to bro{target=browser}`, not to `to *` or a plain entry.
Naming placement explicitly, including `placement=*`, opts into joins.
A join shares the starter's tree and container and is not a security boundary.
Give-only permission prevents direct use, not instructions to an allowed recipient that can use it.

Patterns cannot name overridable options: credentials, `add`, or `drop`.
Those options must remain authority-neutral so changing them cannot change a base or invalidate an earlier authorization.

## Shipped type declarations

Open options have unrestricted entries in every bro base, unless a cap removes or narrows them.
Guarded options need their own authority.
The defaults below are summon defaults; a root launcher and a manual commit explicitly supply their mode/flag choices for placement, harness, hold, and timeout.
Defaults never depend on the starter's authority.

### Bro

| Option | Domain | Default | Open | Overridable |
|---|---|---|---|---|
| `target` | Bro names `[a-z][a-z0-9-]*` | Required | No | No |
| `placement` | `boxed`, `unboxed`, `join` | `boxed` | No | No |
| `harness` | Harness names | Reading's summon harness | Yes | No |
| `provider` | Provider names | Resolved recipe | Yes | No |
| `model` | Strings | Resolved recipe | Yes | No |
| `passthrough` | Flag | Send model and content unchecked by the model-input table | Yes | No |
| `hold` | `unattended`, `detached`, `attended`, `guided` | `unattended` | Yes | No |
| `talk` | Subset of the four talk rights | `[worker.say]` | Yes | No |
| `timeout` | Positive whole seconds or `none` | 1800 | Yes | No |
| `export` | Flag | Unset | No | No |
| `cred.<kind>` | Instance names, including empty | Held kind's configured pick | No | Yes |
| `add`, `drop` | Subsets of registered kinds | `[]` | Yes | Yes |

The four talk rights are `owner.say`, `owner.question`, `worker.say`, and `worker.question`.
The wire mapping adds `worker.say` to requested talk.
Recipe selection follows [ride's home-layer rules](ride.md#recipe-selection).
The child's home-layer stack supplies its configured recipe and credential picks.
The ungoverned `llm` field, when present, replaces that recipe whole with complete recipe text.
The descriptor's `provider`, `model` and `passthrough` describe the resolved recipe, including a configured one.
Caps refuse that recipe rather than changing it, and admission refuses a descriptor that differs from it.
A requested recipe the harness cannot run fails rather than switching harnesses.

A start holds the configured kinds for its target, harness, and provider, plus `add`, minus `drop`.
Configured kinds include recording, harness authentication or the provider's LLM key, and all component needs possible under every credential-gate outcome, with configuration grants and revokes applied.
This includes needs of alternatives or inherited entries uncovered by a closed gate, not just the all-gates-open reach.
Every held pick is checked by the use rule even if only an unselected component needs it.
When the start runs, only selected components' required and optional kinds are hydrated.
An optional kind is skipped only when unpicked and its empty instance is absent.
An explicit `cred.<kind>` holds that kind at that instance without requiring `add`.
A kind cannot be both explicitly set and dropped.

Credential picks follow the home-layer stack, over store `defaults`, over the empty instance.
An explicit request or operator pick overrides those defaults.
A configuration pick selects an instance without adding a kind; a request's `--cred` or `--pass` both selects and holds it.
`prompt`, `into`, `share`, `step_id`, and `index` are ungoverned content.
The existence check verifies the target and harness are installed, the used instances exist, and `into` resolves.
An uninstalled target normalizes without declared needs or a recipe and fails only after authorization.

### Webview

| Option | Domain | Default | Open |
|---|---|---|---|
| `vnc` | Flag | Unset | No |
| `vnc_port` | 1024–65535 or `auto` | `auto` | Yes |
| `downloads` | Flag | Unset | No |
| `cred.cookies` | Instance names | Unset: no profile | No |
| `timeout` | Positive whole seconds or `none` | `none` | Yes |

A requested port needs VNC; `auto` denotes an automatically selected port.
No option is overridable, and the base is empty: a webview starts nothing.
`allowed_origins` and `blocked_origins` are ungoverned content, not permission boundaries.
The browser's navigation filter does not block all top-level redirects.

### Benchmark

`timeout` is open, positive whole seconds, defaulting to 12 hours.
The workspace-relative `config` path is ungoverned content.
The base is empty and no option is overridable.

## Readings, bases, and configuration

A root takes one reading after runtime re-exec and attachment resolution.
It includes the installation's home and settings, applicable host home layers, store defaults, attached-tree sections, and installed bros, types, kinds, harnesses and providers.
Path attachments read the working-tree `pyproject.toml`; URL attachments read it at the resolved commit.
Every descendant and manual commit uses this reading.
Editing configuration cannot change a live tree or break its next summon with a malformed edit.
A resume takes a new reading.
Instance existence, credential material, and minting-source annotations stay live, outside the reading.

A bro's base folds these layers in order:

1. Universal entries: every open option's `*`, the `bro` start permission, and `bro.placement=boxed`, all with full use and give.
   Configured-credential entries permit a target's configured picks only in starts of that target under the matching harness and provider; they give nothing onward.
   There is one entry per configured kind and instance, whose use pattern carries a `configured=<kind>+<instance>` condition decided against the concrete start;
   the family is fixed by the reading, independent of `add` and `drop`, and does not grow with targets, harnesses, or providers.
2. The persona's `may_summon` entries as `@<bro>`, and its `may_launch` entries as type start permissions, with full use and give.
3. Installation home settings, host `defaults`, `homes.<installation home>`, the target's foreign home bro entry when applicable, then the installation home's bro entry.

Each configuration layer applies `revoke` and `limit` caps first, then its `grant` entries.
A later layer can therefore restore an earlier removed right.
Overlapping grant and revoke permissions in one layer fail.
`grant` adds entries; `revoke` takes plain permissions and removes both use and give; `limit` takes qualified entries and retains only the named use and give for the atoms it covers.
Atoms no limit mentions remain unchanged.
A plain grant in a base gives full use and give.
On a summon, a plain grant instead forwards full use with the starter's own recipients for that permission; a qualified grant supplies exactly its clauses, bounded by the gift rule.
Bare credential kinds in grants/revokes become `add`/`drop`, not gifts/caps over credential option atoms.

Home settings cannot choose host instances: they refuse credential-instance permissions and picks.
Host home and bro entries may contain `creds`; host `defaults` may not.
Owned configuration validates fully at root start, even bro layers not used by that root.
Installed foreign bro entries are shape-checked at root start and bound when used;
a winning scalar, recipe or pick shadows an earlier foreign value, while authority folds every layer.
Other homes' unrelated entries are never read.
Authority may name absent instances without failing a launch that does not use them.

The host-only `operator` object accepts `revoke` and `limit`, capping the operator's initially unrestricted authority O.
Every root start, manual commit, override, and resume is checked against O.
It guards the operator against mistakes; it does not introduce multiple operators or ownership boundaries.
The host configuration schema are in `bro/setup/AGENTS.md`, "Host config".

### Auxiliary LLM credentials

Spell dispatch, query-focused source summaries and the browser page reader use the optional `mu` credential, not an agent's provider key.
Without it, spells remain available as tools, source fetches require `query` to be omitted, and `browser::look` is unavailable.
There is no fallback to a provider credential.

Create `~/.bro/creds/mu.cred` with mode 0600.
Its material contains a default `llm` recipe in the `--llm` grammar and the provider's `credential` material, normally a `$cred` reference.
An optional `roles` object maps caller roles (`dispatch`, `summary`, `reader`) to complete `{llm, credential}` pairs.
An undefined role uses the default pair; a malformed entry errors rather than falling back.
The recipe must name a provider with a client-driven wire supporting structured one-shot calls.
For example:

```json
{
  "llm": "openai:gpt-5.6-terra,reasoning_effort=low",
  "credential": {"$cred": "openai+"},
  "roles": {
    "summary": {"llm": "openai:gpt-5.6-luna,reasoning_effort=low", "credential": {"$cred": "openai+"}},
    "reader": {"llm": "openai:gpt-5.6-luna", "credential": {"$cred": "openai+"}}
  }
}
```

References name instances explicitly (`openai+` for the empty instance, `openai+work` for a named one).
A kind-only reference resolves against the hydrating store's selection for that kind, not an operator pick the session does not hold.
Cacheable referenced material is inlined into the session copy of `mu`.
Holding `mu` therefore exposes the credential material its entries reference.
Select a named configuration with `creds: ["mu+cheap"]`; callers choose roles, never instances.
Drop `mu` to disable these auxiliary routes, or select an instance whose entries use the desired provider.

## Start surfaces

| Surface | Values | Gift and cap |
|---|---|---|
| `ride along`, `ride solo` | Mode flags, `--llm`, `--harness`, `--cred`, credential `--grant`/`--revoke` | Permission `--grant`, `--revoke`, `--limit` |
| `summon` | Target, placement flags, `--pass`, credential `--grant`/`--revoke`, `--harness`, `--llm` | `--grant`, `--revoke`, `--limit` |
| `bro::summon` | `target`, `placement`, `passes`, `harness`, `llm`, `hold`, `timeout`, `talk` | `grant`, `revoke`, `limit` lists |
| `launch {type: …}` | Type's wire fields | `grant`, `revoke`, `limit` lists |
| `webview open` / `webview::open` | VNC, port, downloads, cookies, timeout | Checked as a webview start |
| `benchmark-run` | Config and timeout | Checked as a benchmark start |

`summon --pass github+reviewer` sets `cred.github=reviewer`; it does not give onward permission to pass it again.
A summon may grant or revoke bare kinds, but the resulting credential defaults must pass the use rule like explicit picks.
The CLI's `--join`, `--boxed`, and `--unboxed` map to `placement=join`, `boxed`, or `unboxed`.
`--start` selects a separate party; an otherwise unmarked summon always defaults boxed, never falls back to unboxed, and never implicitly joins.
A join refuses `into` and `manual` and inherits its party's isolation.

For each start the host resolves the starter's mission record, maps the request, normalizes it under the reading, runs use and gift rules, runs admission checks such as summon depth, records its authority, then checks existence and runs the worker.
Only a starter authorized to name an item learns that it is missing.
A cap on configured credentials may refuse starting the bro unless the request drops that kind.
Artifacts are outside this model except for publishing to the host:
a mission can share only refs it can already read, and `bro.export` is the one permission a session needs to publish a ref it can read to the operator, who reads it with `ride artifact get`.
The operator needs no permission to hand a host file or an exported ref to any live mission.

## Manual children: joint starts

`summon --manual` prepares a request without starting a process.
It accepts target, talk, credential passes and kind additions/drops, gift, cap, prompt, and `into`.
It refuses placement, hold, LLM, harness, timeout, and `share`, which belong to the launch or need a workspace.
The host checks the start atom and set values with prepare-time matching, treating conditions on unset options as met, and bounds the gift's onward recipients.
A manual summon needs no placement right: the operator selects placement at commit.

`ride along|solo --summoned <token> <target>` re-executes into the root's runtime and commits over the operator channel.
The operator supplies placement, hold, recipe, harness, and credential flags, replacing starter credential values when specified.
The host recomputes defaults under the root reading, validates the final descriptor, and repeats authorization against current authorities.
Starter values and defaults are checked against the starter, operator values against O, with all patterns matched against the final descriptor.
The gift rule runs on that final descriptor too.
The launcher hydrates exactly the admitted descriptor's picks, including authentication and LLM credentials, without rereading host policy.

The CLI exposes less than the abstract joint-start model: the target positional must match; changing talk, prompt, `into`, `--env`, or authority entries at commit is refused.
Use `ride override` for supported authority changes once the child runs.
The commit returns an opaque generation; claiming must match that generation and workspace.
An unchanged successful retry retains its generation, while a superseded launcher is refused before execution.
The runtime, resume record, and host record therefore refer to one committed launch.

## Running-session overrides

From the host, use `ride missions <root>` to find mission ids, then:

```sh
ride override <mission> --grant @browser
ride override <mission> --cred github+reviewer
ride override <mission> --revoke github
```

Only concrete `@<bro>` names and bare credential kinds are accepted by `--grant`/`--revoke`; `--cred` selects and holds a kind.
No other authority or option can change mid-run through this CLI.
The operator can change any running bro in the tree, not only the root.
The root must remain alive; a detached manual child has no host.
The command re-executes into that root's runtime and refuses roots without its operator control record.
The operator channel's token/runtime file is mode 0600 under `<runtime-root>/control/` and is never mounted in containers.
Mission channels cannot issue operator requests.
Like unboxed scoping, this is not a boundary against processes running as the host user.

### Authority changes

For a named target, `--grant` puts it in the operator gift and removes its operator cap entry; `--revoke` does the reverse.
The latest flag for a name wins.
The operator layer can restore a target removed by the starter's cap.
The host checks the candidate against O and persists the operator layer and operation id atomically in the resume record, or joined-member `override.json`.
That write commits this no-applier operation; the host then publishes its pending record and journal transition synchronously, without a second clearing write.
If persistence fails, the override is refused.

The session watch reports the change.
A session with no initial targets receives its saved summoner contract at its first grant, together with the turn-end fragment if needed.
Its tool roster does not change: `quest_watch` remains available only where mounted at start, but the runtime session watch is armed in every brokered session.

### Credential changes and settlement

Credential flags update `add`, `drop`, and `cred.<kind>`.
The host normalizes under the mission's unchanged reading, checks changed values against O and instance existence, and asks its supervisor's applier to publish them.
Root/spawned supervisors publish into their private root or container; joined members have their own store and install directory; a manual launcher keeps an applier connection.
Only changed kinds are staged, so unrelated kinds keep working at their stable paths.
See [store publication](ride.md#session-permissions-and-credentials) for index generations, hooks, environment overlays, and consumer refresh.

Every credential operation has an id and pending candidate values in the mission record and its resume record, when it has one.
The applier is idempotent per operation id.
After all publication steps, it writes a durable host-side receipt naming the operation and generation, then acknowledges.
Only then does the host commit the candidate in its record, resume record, and journal and reply `applied`.
A timeout replies `uncertain` with the operation id, not a claim of failure or success.
While pending, another override of that mission is refused naming the operation; the mission's own starts continue.

Settlement uses the receipt, never just the store generation: the index may be committed before hooks finish.
A reachable applier finishes publication and acknowledges; after mission end, or at the workspace's next resume if its root is gone, a receipt commits the candidate and no receipt drops it.
A manual applier holds a host lifetime pipe separate from reconnectable TCP.
Claimed work stays pending through mission end until a receipt arrives or that pipe reaches EOF, so a lost link does not discard a late receipt or strand it.
Joined members have no resume record: pending operations and receipts live in member records, and unclaimed pending work is dropped when the member ends.

Spawned workspaces with credential-operation records survive even a clean exit, preserving resume/receipt settlement after the session ends.
Their private credential stores still tear down.
`ride clean` is the record-removal path; clean completion alone does not remove these workspaces.

An override cannot retract material already read.
It updates later store reads and newly started commands, not tools/features selected at start, the prompt, or the tool roster.
Processes that latch credentials at start keep them until the next start, including Claude authentication, the bro harness's LLM client, and the recorder's trails client.
The reply names each changed kind's next-start-only effects.
Credential-derived caches refresh on store generation; resource-owning toolsets retain resources such as open browsers.

## Resume and durable records

`ride resume <workspace>` is a new operator root start with a new reading.
It carries recorded set values (all attributed to the operator), gift, cap, and operator layer, then applies defaults again.
Default credential picks follow current configuration;
the resolved-recipe record keeps the recipe and source the session ran, unless `--llm` replaces them.
`--cred` and credential grants/revokes replace per-kind values; permission flags replace per-name operator-layer entries; `--limit` applies caps.
All start checks run against O.
A resumed child is a root; its former descendants do not survive.
Joined members have no independent resume record.
A resumed manual child's runtime is frozen from the invoking installation rather than retaining its former root's bundle.

The host record keeps type, set values with principal attribution, normalized descriptor, gift, cap, operator gift/cap, and effective authority.
The reading belongs to the root tree.
Resume records keep set values and the resolved-recipe record, not other resolved defaults, and retain pending credential operations until settled.
Summon depth and summon harness come from the new reading on resume.
The launch audit records descriptor, gift/cap, and refusals with their rule and atom.
Records without a resolved-recipe record are refused, not migrated, and `ride list` still lists them.
The refusal names the workspace and recovery routes: fork its native trail with `bro chat <bro> --fork <trail>`, or start a new session.

## Visibility and refusals

`ride scope` reports a prospective descriptor, base authority, credential picks and their `PRESENT`, `SKIPPED`, or `MISSING` state, plus presence of authority-named instances.
It does not fail merely because an instance named by authority is absent.
It is prospective configuration, not an override of a running mission.

A mission's broker `authority` request returns its live descriptor and authority.
The banner reads it live: `may_summon` shows usable bro targets and conditional entries, `permits` other usable entries, and `may_give` give-only entries with recipient restrictions.
Spell casts read current authority; prompt composition and its `#may_summon` fact read it at start.
A process without a channel, such as `ride exec`'s shell, a broker-disabled root, or `bro show`, has no authority rows.
The host's mission record enforces authority, never the process environment or banner text.

A refusal identifies the rule/check and atom/option: declaration/domain, validity, use, gift, admission, or existence.
Check the whole descriptor, including defaults, when diagnosing use failures.
A missing placement right does not cause fallback; request the authorized placement explicitly.
A gift failure means either the final recipient is outside the starter's recipients or its onward give is too broad.
Existence errors occur only after authorization succeeds.
