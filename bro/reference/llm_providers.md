# Adopting an LLM provider or model

A provider declares recipes, credentials and prices; a wire implements the protocol those recipes use.
The native harness and auxiliary `mu` calls open wire clients through the same roster.
A harness that drives its own loop can serve a provider without a native wire client.
A provider is selected by recipe, independently of the persona: adding one does not require another bro.

## Provider contract

Use [DeepSeek's distribution](../../llms/deepseek/pyproject.toml) and
[declaration](../../llms/deepseek/bro/deepseek/declaration.py) as a concrete example.
The shared interfaces are [Recipe](../llm/llm.py) and the [provider roster](../llm/providers.py).

- Register the declaration module in `bro.llm.providers` and the credential kind in `bro.credentials`.
  The provider entry name follows `[a-z][a-z0-9-]*` and equals the recipe's `TYPE` and credential kind.
  Duplicate entry names fail rather than establish a priority.
  Entry points require an environment sync after they change.
- Export a frozen dataclass `Recipe` inheriting the shared base, with a `model` field and provider-native fields.
  Each provider field has a default and a name matching `[a-z][a-z0-9_]*`, other than `type`, `model` or `passthrough`.
  Its type is `bool`, `int`, `str`, a `Literal` of writable strings, or one of those unioned with `None` with default `None`.
  An unset optional field leaves the vendor's own behavior in force.
  The inherited `__post_init__` validates types and refuses values recipe text cannot write:
  an empty model or one containing `+`, `,` or whitespace, and an empty string field or one containing `,` or whitespace.
  Additional combination checks call `super().__post_init__()` first.
  The base owns serialization; providers implement no record methods.
- The class supplies `TYPE`, `WIRE`, `ENDPOINT` when needed, and the class method `needed_secrets()`.
  A client-driven recipe supplies `request_options()` and `is_transient(error)`.
  Discovery reads wires and keys from classes without building recipes.
  Every recipe class defines `MODEL_INPUTS`, mapping listed model ids to `frozenset`s of lowercase MIME types without parameters, copied from the vendor's model and input-format guides.
  Text is always accepted and is not listed in the table.
  The base `accepts(mime_type)` accepts every MIME type under its keyword-only `passthrough` flag, otherwise checks the model's row; do not override it or redeclare the flag.
  Options must not overlap the wire client's `OWNED` fields; unsupported input raises `UnsupportedContent` before sending.
- The class also defines `RETIRED_MODELS`, mapping retired model ids to `datetime.date` retirement dates.
  A model cannot be both listed and retired.
  `PRICE_TABLE.models` must hold exactly the listed and retired models, so delisting never discards historical prices.
  Declaration loading validates these tables and the provider field schema, naming the offending model or field.
  Export `FAILURE_SIGNATURES` using the neutral failure categories in `llm.py`.
  Pricing exports are `PRICE_TABLE`, `PRICE_TABLE_SHA256` and `price(model, usage, service_tier, table)`;
  add `price_table_from_content` for retained benchmark tables.
  Record source and effective date, use the provider's raw billing vocabulary, and leave unknown models unpriced.
  Export `MODEL_SLUG` and `model_family(slug)` only when the generic dated-slug label is insufficient.
- The launch selection API also requires `DEFAULT_MODEL`, `MODELS` (short names to full ids), and `MODEL_ID` when verbatim model ids need a pattern.
  The default and alias targets must be listed in `MODEL_INPUTS`.
  `selection_fields`, `deferred_fields` and `effort_values` support selection and reconstruction;
  `with_effort()` maps neutral levels onto the provider's documented scale, and `fast()` is implemented only when supported.
  This selection API is separate from complete recipe text below.
  `require_admitted` checks the final recipe at authority settlement, native launch and wire opening;
  a listed selection can override an unlisted declaration.
  The deferred Anthropic session model remains admitted for this API.
- `known_names()` reads sorted metadata without imports.
  Explicit selection loads only the named declaration and fails by name if it is unavailable;
  searches warn and skip broken declarations, without breaking unrelated launches.
  Stored recipes resolve by their `type` through `Recipe.from_dict()`.
  A native launch requires a client entry for the recipe's wire;
  an entry alone does not install that client's SDK.
- Contribute the key as a credential kind, normally with material `{"api_key": "…"}`;
  `<provider>+<instance>` selects a named instance.
  A missing required key refuses the run at startup.
  Keep the package hub import-free and the credential module an import-free literal dictionary:
  registry assembly loads every credential contribution.
  Declarations import no harness and no SDK at module level;
  import SDK exception classes inside `is_transient()` instead.

### Recipe text and records

The complete text API in `bro.llm.providers` uses `provider:model[+passthrough][,field=value]...`.
It names the model verbatim, not a short name, and the fields under their provider-native names.
The text splits at its first `:`, allowing further colons in a model id.
Flags precede fields; `passthrough` is the only framework flag, and neither flags nor fields may repeat.
Whitespace, empty slots, unknown fields and values of the wrong type are refused with `RecipeError` naming the text and offending part.

- `parse_recipe(text)` checks only grammar and returns provider, model, flags and assignments as written, without loading declarations.
- `bind_recipe(text)` loads the named declaration, reads fields by their annotated types, constructs the recipe and requires admission.
  Booleans are `true` or `false`, integers are decimal digits with an optional `-`, and `Literal` values must be members.
  An omitted field keeps its class default; no text sets a field to `None`.
- `format_recipe(recipe)` writes canonical text:
  the provider and model, `+passthrough` when set, then fields differing from defaults in declaration order.
  For every admitted recipe with a concrete model, binding its canonical text rebuilds the same recipe.

For example, `openai:gpt-5.6-terra,service_tier=priority,reasoning_effort=high` canonicalizes to `openai:gpt-5.6-terra,reasoning_effort=high,service_tier=priority`.
Anthropic's native fast knob is `fast_mode=true`; `+fast` is not a framework flag in this grammar.
Launch flags and configuration still take the selection API described under [operator adoption](#operator-adoption), not this complete text API.

`Recipe.dump()` stores `type`, `model` and every provider field, adding `passthrough` only when true.
`Recipe.from_dict(data)` requires `type` and `model`, uses class defaults for omitted fields and refuses unknown keys or wrongly typed values.
It loads only the named declaration and does not check admission, preserving retired-model records.
For example, an older OpenAI record missing `compact_threshold` loads with that field unset.
A retired recipe still has canonical text, but binding it without `+passthrough` refuses it with its retirement date and the provider's listed models.

### Installing the client extra

A provider depends on its wire distribution **bare** and, when client-driven, offers its own `client` extra requesting the wire's `client` extra.
Only the wire's `client` extra carries the SDK dependencies.
An installation driving the provider, whether through the native harness or `mu`, installs `<provider-distribution>[client]`.
An installation only reading declarations, prices or trails takes the bare provider.
For example, `bro-deepseek[client]` requests `bro-wire-chat[client]`;
`bro-deepseek` alone can resolve and price recipes and decode their records without an SDK.
`wires.open()` reports the missing wire extra when the SDK is absent.

This checkout's workspace sync takes all extras (`./setup.sh`).
The [benchmark bundle builder](../../benchmark/bro/benchmark/bundle.py) exports with `--extra client` and verifies registered clients load;
its outer Harbor environment installs bare providers.
A consumer application that runs the native harness or auxiliary calls must request the provider extra in its own dependencies.

## Isolation

Framework code names no provider, wire or harness as a built-in choice, imports no plug-in package,
and holds no literal equal to a provider name without a direct dependency on the contributing distribution.
Resolve providers through `bro.llm.providers`, wires through `bro.llm.wires`, and harnesses through `bro.harnesses`.

A distribution may name a plug-in it directly depends on:
its own provider package, a provider's wire, or a harness's supported provider.
Core depends on no workspace member.
Tests and documentation may name examples.

The [provider policy test](../../local/bro/local/provider_policy_test.py) derives plug-in packages and provider names from workspace entry points,
checks imports and provider literals against direct dependencies, and enforces SDK-free bare provider/wire dependencies.
Wire and harness name literals are common words and need review rather than blanket literal matching.

## Choosing or adding a wire

Choose an existing wire only when its request encoding, response decoding, tool protocol, usage vocabulary and replay rules fit the service.
Sharing an SDK or an API's marketing name is not enough.
The [wire roster](../llm/wires.py) loads decode modules from `bro.llm.wires` and client classes from `bro.llm.wire_clients`;
[extending](extending.md#entry-point-groups) lists the registration groups.

A new protocol needs a distribution under `wires/<name>/`, with an import-free package hub and these two sides:

- **Decode:** export matching `NAME`, `CHAINING` (`server` or `client`), `decode(response)` and `counts(usage)`.
  Depend only on core, without module-level SDK or harness imports.
  `LEGACY` claims untagged records only when that protocol actually owns the historical records;
  several claimants are an error, so a new wire must not claim them merely as a fallback.
  Decode into [DecodedCall](../llm/records.py): ordered reasoning, messages, calls or `Unknown` parts,
  raw usage, model, service tier, refusal and the neutral stop with its vendor reason.
  Preserve unfamiliar shapes as `Unknown`, not a decoder exception or a fabricated valid call.
  Empty or whitespace-only call arguments parse as `{}`; non-object arguments remain invalid for the loop to answer as a tool error.
  `counts()` maps billing onto the four classes of [usage](../llm/usage.py).
- **Client:** register a class implementing [Wire](../llm/wire.py), with its SDK in the distribution's `client` extra.
  It accepts the recipe and credential material, encodes neutral inputs and tools, owns `OWNED` request fields,
  and implements `exchange`, `restore`, and `structured(instructions, result, content, request_timeout)` for auxiliary one-shot calls.
  User content is a nonempty tuple of `Text` and `File` from `bro.llm.wire`.
  Call `require_accepted(recipe, content)` before encoding any item, then refuse any model-accepted type the protocol cannot carry with `UnsupportedContent`.
  One `Text` encodes as string content;
  multiple items or a file encode as a list.
  Read no file in a wire;
  `File` carries bytes and a declared MIME type.
  Replay recorded user and notification text through the same encoder as `UserMessage((Text(body),))`.
  A server-chained client may implement `seed_chain`; a client-chained wire must reject it.
  Build each `Leg` from the same raw JSON passed to decode and counts, retaining the raw response, nulls included.
  A wire's decode and client sides share a version.

### State, failure and replay

Record an exchange's **new inputs only**, even when a client-chained request sends the whole history.
Tag every `llm_call` with the wire's `NAME`.
Restore from the system prompt and the steps' own user/notification text, raw assistant responses and raw tool outputs,
not from recorded request histories.
Strip response-only fields as the protocol requires and preserve fields that must be passed back.
Same-wire forks may change provider or model; cross-wire forks are refused.

Map vendor stops to `completed`, `truncated`, `filtered`, `capacity` or `failed`.
Non-completed legs do not advance conversation state;
Loop records them with `stopped`, counts their usage, emits no accepted-response events, executes no calls and keeps their inputs pending.
Capacity responses use [the shared retry driver](../llm/retry.py), with every loop attempt recorded and accounted;
other uncompleted legs, and exhausted capacity retries, raise `LLMStopped`.
`structured` uses the same retry and stop rules, but contributes no loop usage.

A completed response without calls ends the turn even when it carries no text:
its reply is empty, and the session's turn-end rule decides what follows.
A completed response that the client cannot use raises `UnusableResponse` with the received leg and explicit `accepted` status.
That status says whether the wire advanced its conversation state.
If rejected, Loop records the leg with `stopped: failed`, counts it and keeps the exchange's inputs pending without emitting accepted events.
An accepted unusable reply is recorded and accounted but acknowledges its inputs.
Fork-point selection and restore omit steps carrying `stopped`, while replay retaining input steps allows continuation after a rejected reply.
Records without that marker replay as accepted history.

### Conformance and server installation

Add fixtures to the shared [wire suite](../../harnesses/native/harness/bro/native/wire_test.py)
and [fixture adapter](../../harnesses/native/harness/bro/native/wire_fixtures_test_helper.py).
Cover plain replies, replies without text, reasoning, interim text with calls, valid/empty/malformed arguments,
usage, every stop, record/restore/next-request equivalence, and call indexes matching projected tool-call indexes.
Also exercise cancellation, rejected completions and pending inputs, initial prompt ordering, and same-wire fork/resume.
Wire-specific tests cover the protocol's own constraints.

Add the decode entry to the trails-server image's [declared entries](../../oops/trails/server/entries.json).
Its [policy test](../../oops/bro/oops/trails_deployment_test.py) compares that list with workspace registrations (excluding the diagnostic wire);
the build stages the distributions providing the entries, and startup refuses an entry that cannot load or an invalid legacy claimant set.
Extend [image cases](../../oops/trails/server/image_cases.json) and [image verification](../../oops/trails/server/verify_image.sh) for the new protocol.
Deploy the decoder before new wire records are written, and redeploy it when decoding changes, before reading affected records.
The server stores rows verbatim and projects on read;
an unknown wire degrades to a forensic event without losing its record.

## Adoption checklist

1. **Optionally run an early API canary.** Check the intended capability, tools and thinking-mode pass-back requirements,
   response shapes, supported content and schema limits, error codes, rate limits and stop vocabulary.
   This is evidence, not a prerequisite to building the plug-in.
   Without a key, start from documented vendor shapes and keep the unverified claims explicit.
2. **Add the distribution.** Create `llms/<provider>/` with `pyproject.toml`, an import-free package hub and the modules in the provider contract.
   For a new wire, register its distribution as well.
   Use [DeepSeek's metadata](../../llms/deepseek/pyproject.toml) and [Chat's metadata](../../wires/chat/pyproject.toml) as examples.
   Register each member in these checkout-owned places:
   - root [pyproject.toml](../../pyproject.toml): workspace members, pytest `pythonpath`, pyright `include` and `extraPaths`, ruff `src`;
   - root [AGENTS.md](../../AGENTS.md): distribution map, with the member's own `AGENTS.md`;
   - [local/pyproject.toml](../../local/pyproject.toml): dependencies, workspace sources and package-module mapping;
   - native [distribution_test.py](../../harnesses/native/harness/bro/native/distribution_test.py): built-wheel and entry-point coverage;
   - [run_tests.py](../../local/bro/local/run_tests.py): `DISTRIBUTIONS`, unit files and the opt-in live probe in `LLM_PYTEST_FILES`;
   - [environment_policy_test.py](../../local/bro/local/environment_policy_test.py): source sweep coverage (the `llms` and `wires` directory families are already included);
   - root [uv.lock](../../uv.lock): relock and sync with all packages, groups and extras so entry points are installed.
3. **Declare credentials and recipes.** Add the import-free credential dictionary, model aliases, id shape, effort mapping,
   provider request options, transient-error classifier, failure signatures and provenance-bearing price table.
   Round-trip recipes and tables, test invalid inputs and content rejection, and verify bare installation imports no SDK or harness.
   [DeepSeek's declaration tests](../../llms/deepseek/bro/deepseek/declaration_test.py) and the distribution tests exercise these boundaries.
4. **Exercise the real routes offline.** Capture fixtures from live responses when a key is available;
   otherwise write fixtures from the vendor's documented shapes and identify that provenance.
   Test decode, usage and prices, a multi-leg tool loop and required history pass-back, terminal and transient HTTP errors,
   capacity retries, projection, fork/resume and `ride scope` selection.
   Also summon a persona configured on another provider onto this provider, from a host that holds the other provider's key,
   and run it with the real credential gate:
   it must start without that key.
   For a provider that can serve auxiliary roles, hydrate a `mu` entry naming it into that stage and call it:
   the one-shot must reach the endpoint with no other provider's key material in the session.
   [DeepSeek's route tests](../../llms/deepseek/bro/deepseek/routes_test.py) use an in-test HTTP server rather than a vendor key.
5. **Provide the live probe.** Add an opt-in `<provider>_llm_test.py` that saves raw responses for refreshing offline fixtures,
   and register it in `LLM_PYTEST_FILES`, never the ordinary unit roster.
   Probe no-argument calls, usage and returned model ids, the full native tool roster, required history fields,
   mode transitions and a forced truncation.
   [DeepSeek's probe](../../llms/deepseek/bro/deepseek/deepseek_llm_test.py) is the worked artifact.
   Run it only where the key and client extra are installed; retain the raw evidence and refresh fixtures from it.
6. **Package any benchmark preset.** If a preset names the provider, add its bare distribution and workspace source path to
   [benchmark/pyproject.toml](../../benchmark/pyproject.toml), and its wheel and any new wire wheel to the bundle's `WHEEL_PACKAGES`.
   The bundle export requests the client extra as described above; Harbor's process still needs the bare declaration for post-run analysis.
   A preset using cast also needs a `mu` instance.
   Relock both independent projects, `benchmark/` and `oops/cdk/`, as well as the workspace;
   `./setup.sh` does not sync their environments.
   Sync those in use and rebuild the benchmark bundle so installed metadata and wheels match the preset.
7. **Update navigation and verify.** Update this page's examples when the procedure changes, the member maps,
   [README](../../README.md), [bootstrap checklist](../../BOOTSTRAP.md) and any affected operator examples in
   [ride](ride.md), [permissions](permissions.md) and the [permission cheat sheet](permissions_cheatsheet.md).
   Run the formatter, focused tests and wheel builds, then the repository gate against the delivery branch.
   A new decode side also needs the server checks above.

## Adding a model to an existing provider

Use the existing declaration: add a `MODELS` alias, a `MODEL_INPUTS` row copied from the vendor’s input-format documentation, and a price row, and check the vendor's returned snapshot ids remain priced.
Add newly documented filename suffixes to `EXTENSION_TYPES` in `bro.llm.wire`, choosing a MIME type every vendor accepting that format lists.
When no single type serves them all, leave the suffix out and require callers to pass `mime_type` explicitly.
Test knob defaults, effort mapping, content support, usage and pricing for any model-specific differences.
A new model need not require a new wire, credential or provider distribution.
Update and run the provider's live probe, refresh captured fixtures, and adjust presets and operator recipes that select the model.
An unlisted model is refused unless the recipe carries `+passthrough`, which also bypasses its model-input checks, but not the wire’s protocol constraints.
To retire a model, remove its aliases and `MODEL_INPUTS` row, add its id to `RETIRED_MODELS` with the date the framework stops admitting it, and keep its price row.
OpenAI's dated snapshot ids remain priced through its table's snapshot mapping.
Models outside both tables can run under `+passthrough` but remain unpriced.

## Operator adoption

Before re-syncing an installation, move configured unlisted models to listed ones in host home layers and `mu` materials.
A kept workspace whose recorded selection names an unlisted model cannot resume after the re-sync; see [ride](ride.md#bro-harness) for continuation.
End or cancel running Harbor jobs before re-syncing, and start none until the benchmark bundle is rebuilt.
Re-sync every installation reading the shared configuration, rebuild the benchmark bundle, and start fresh roots; move consumer repositories’ framework pins when they adopt the content routes.
Only after every such installation has moved and every root from before its re-sync has ended may `+passthrough` appear in shared configuration or benchmark presets.
An older installation refuses that suffix; a root and all its children retain the runtime frozen when it started.

1. Install the provider with the client extra wherever it will run, and the decode side on trail readers.
   In this checkout, sync the workspace and any independent projects in use;
   deploy new or changed server decoders before use, as above.
   Start a fresh root after re-sync: a root and its children keep the runtime frozen at the root's launch.
2. Fund the account and accept the service's data terms before sending repository or tool content.
   Enter the key directly into the host credential store, with mode 0600 and no terminal echo.
   Do not put key material in a prompt, repository, issue, trail, summon argument or `--env` flag.
   Credential selection arguments name instances, never secret values.
3. Choose a recipe and harness explicitly, through launch flags or configured selections.
   There is no framework default recipe or harness.
   Managed launches and in-process `bro run` and `bro chat` resolve through the installation's home-layer stack.
   The configured recipe is the winning `llm.<harness>` entry, with its source layer.
   An unsupported foreign value fails only the starts that use it, naming the plug-in to install and the installation's own bro layer as an override.
   See [ride](ride.md#recipe-selection) for flags and harness selection.
4. Check without starting a session:
   `ride scope --bro bro-dev --harness bro --llm deepseek:pro:high` must show the selected provider and key presence.
   Then run the opt-in live probe before a full task, and confirm a recorded tool loop and same-wire resume.
   Do not infer capability from an offline fixture pass.
5. For auxiliary calls, configure a `mu` credential with a default `{llm, credential}` pair and optional role pairs.
   Roles `dispatch`, `summary` and `reader` select within that material;
   explicitly name credential instances in `$cred` references so hydration does not depend on which other kinds the session holds.
   See [auxiliary LLM credentials](permissions.md#auxiliary-llm-credentials) for the material and selection rules.
   The provider needs a working `structured` implementation; nothing falls back to another provider key.

## Worked example: DeepSeek and Chat

The two plug-ins are [bro-deepseek](../../llms/deepseek/pyproject.toml) and [bro-wire-chat](../../wires/chat/pyproject.toml).
The provider's [declaration](../../llms/deepseek/bro/deepseek/declaration.py) supplies the endpoint, models, thinking options,
content capabilities, retry classification and peak-price table;
its [credential dictionary](../../llms/deepseek/bro/deepseek/credentials.py) registers the key without importing the declaration.
The wire's [decode side](../../wires/chat/bro/wire_chat/decode.py) owns Chat response parts, stops and counts;
its [client](../../wires/chat/bro/wire_chat/client.py) owns history, tools, JSON-mode structured replies and reasoning pass-back.
Neither the native loop nor core needs a DeepSeek-specific branch.

Chat is client-chained: every live request resends accepted history, but each record stores only new messages.
It retains `reasoning_content`, tool calls and the `content: null` beside them when replaying;
an assistant turn without tool calls is sent with string content, empty when the reply had none.
It normalizes the initial system prompt to the head, including when the first exchange contains an inbox notification.
Function calls missing a nonempty string id or name decode as `Unknown`, retaining forensic evidence without projecting invalid calls;
such a completion is rejected without advancing history, so its inputs survive continuation and replay as described above.
The route tests compare live continuation and restored requests through those failures and through a silent turn end.
The live probe saves evidence for the protocol details that documented fixtures cannot establish.

Select `deepseek:pro:high` for any suitable persona rather than copying it into a provider-specific persona.
The [cross-provider summon example](permissions_cheatsheet.md#run-a-child-on-another-provider) covers credential picks and launch authority.
DeepSeek's costs are peak-rate upper bounds, not timestamp-aware bills;
its data terms and weaker safeguards require deliberate routing of inputs and normal review.
