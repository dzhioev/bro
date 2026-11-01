# Design: model selection — complete recipes from configuration, one resolution

This file is the specification of the refactor that [#1150](https://github.com/dzhioev/bro/issues/1150) carries.
It lives on the branch `design/1150-model-selection`, which is never merged:
the pull request that carries it is a design review only.
It starts from the decisions recorded under `## Step 2 outcome` on #1150 and from #1143 as it landed (`5431fdd2`);
the decision record (§14) lists every decision with its status.

## 1. Goal, scope and constraints

Goal: anyone can say which model a session runs, and why.
A recipe is always complete and written in one syntax;
it comes either from configuration or from an explicit `--llm`;
one function resolves it on every surface;
and the session shows the recipe and the configuration entry it came from.

What it enables, for example:

- In this repository, `ride along bro-dev` on the native harness runs `openai:gpt-6.1-sol,reasoning_effort=high`, because `projects.bro.bros.bro-dev.llm.bro` in `~/.bro.json` names it,
  and the banner says `llm: openai:gpt-6.1-sol,reasoning_effort=high (projects.bro.bros.bro-dev)`.
- The same bro on the Claude harness runs `anthropic:claude-opus-5-5,effort=xhigh` from `defaults.llm.claude`,
  and Claude Code is started with `--model claude-opus-5-5 --effort xhigh`.
- `browser`, summoned from a ppp session, reads its recipe from `projects.bro.bros.browser`, its home project's entry, unless `projects.ppp.bros.browser` overrides it.
- `ride along --llm openai:gpt-6.1-sol,reasoning_effort=max eyebro` runs exactly that recipe;
  `--llm :sol` and `--llm openai` are refused as incomplete.
- `terminal` keeps its context bound through configuration: `openai:gpt-5.6-terra,reasoning_effort=high,compact_threshold=200000`.
- A malformed entry under `projects.ppp` cannot stop a launch from this repository's installation.

In scope:
the recipe representation (syntax, fields, flags, records) and the provider declarations' tables;
the installation's project and each bro's home project;
the host configuration's schema, layering and validation;
the one resolution function and every surface that calls it;
the LLM authority options;
how the resolved recipe reaches each harness and what the session shows of it;
the removal of the mechanisms §11 lists;
records, migration and rollout, ending in an interactive step that writes the new `~/.bro.json`.

Out of scope, each with where it belongs:

- repository attachment (`--repo`, URL mirrors, path and URL bases, detached sessions as the base mode)
  and the keys describing the attached tree (`image-repository`, `build-context-command`, `setup.sh`, `[tool.bro.devoops]`)
  ([#1157](https://github.com/dzhioev/bro/issues/1157));
- authority over the whole recipe, a tool listing the recipes a summoner may pass to a bro, and LLM hints (follow-ups recorded on #1150);
- `--llm` presets, which leave now and return later in another shape;
- restricting model choice inside an interactive Claude session (`/model`), which stays free;
- offering an agent tools according to what its model accepts ([#1046](https://github.com/dzhioev/bro/issues/1046), [#1047](https://github.com/dzhioev/bro/issues/1047)).

Constraints:
core imports no other workspace member, and a provider declaration imports no SDK at module level (`bro/llm/AGENTS.md`);
recorded trails stay readable;
backward compatibility is not a constraint (`AGENTS.md`, "Conventions"), and other installations that read the same `~/.bro.json` stop launching until they move past this design (§12.3).

## 2. Concepts

- **Provider** — a vendor's model API as one plug-in declares it (`openai`, `deepseek`, `anthropic`, `echo`; `bro/llm/providers.py`).
- **Recipe** — a complete choice of how to call a model: a provider, a model id, the framework flags (§3.3) and the provider's fields (§3.2).
  In code it is an instance of the provider's recipe class, a subclass of `LLMSpec` (`bro/llm/llm.py`).
- **Field** — one knob of a provider's recipe class other than the model and the flags, under the provider's own name: OpenAI's `reasoning_effort`, Anthropic's `effort`.
- **Framework flag** — a property of a recipe that the framework itself interprets and no provider declares; `passthrough` is the only one (§3.3).
- **Recipe text** — a recipe written as a string in the grammar of §3.1, the one form a recipe takes on the command line and in every configuration file.
  **Canonical text** is the one recipe text `format_recipe` writes for a recipe (§3.4).
- **Listed model** — a key of the recipe class's `MODEL_INPUTS` table (#1143); a recipe is **admitted** when its model is listed or it carries `passthrough` (§3.5).
- **Harness** — the engine a session runs on (`bro`, `claude`); a harness **serves** the providers named by its `llm_providers` (§3.6).
- **Installation** — one Python environment holding the framework and plug-ins, together with the runtime bundles frozen from it.
- **Project** — a named body of configuration, `[a-z][a-z0-9-]*`: `bro`, `ppp`.
  An installation has exactly one, the **installation's project**, whose **project settings** it carries (§4.1).
- **Home project** — the project of the distribution that registers a bro (§4.2): `browser`'s home is `bro`.
- **Host configuration** — `~/.bro.json`, the host's own layers (§5).
- **Reading** — the snapshot of the installation, its project settings and the host configuration that a root takes at its start (`ride/ride/authority_reading.py`); its summons reuse it (§6.4).
- **Layer** — one object of configuration in the schema of §5.1: the project settings, `defaults`, `projects.<project>`, or `projects.<project>.bros.<bro>`.
  A layer's **path** names it: `projects.bro.bros.bro-dev`.
- **Layer stack** — the ordered layers that apply to one bro launched in one installation (§5.2).
- **Resolved recipe** — the recipe a start admits (§6.1), together with its **source**: the path of the layer that named it, or the launch surface that passed it.

## 3. Recipes

### 3.1 Recipe text

```text
recipe   = provider ":" model *( "+" flag ) *( "," field "=" value )
provider = lower *( lower / digit / "-" )
flag     = lower *( lower / digit / "-" )
field    = lower *( lower / digit / "_" )
model    = 1*( any character but "+", ",", and whitespace )
value    = 1*( any character but ",", and whitespace )
```

`lower` is `a` to `z`, and `digit` is `0` to `9`.
The text splits at its first `:`, so a model id may contain further colons.
A model id containing `+`, `,` or whitespace cannot be written.

`parse_recipe(text)` (`bro/llm/providers.py`) checks the grammar alone and returns the provider, model, flags and field assignments as written.
It needs no provider declaration, so an installation can check text naming a provider it does not have.
It refuses, with `RecipeError` naming the text:

- **T1.** text that does not match the grammar, including an empty slot (`openai:`, `:sol`, `openai:sol,`), surrounding or inner whitespace, and a provider alone (`openai`);
- **T2.** a flag or a field named twice;
- **T3.** a flag other than the framework flags of §3.3.

### 3.2 Binding text to a recipe

`bind_recipe(text)` parses the text, loads the named provider's declaration and builds its recipe class:

- **B1.** The model is the text's model, verbatim.
- **B2.** Each field assignment sets the recipe-class field of that name.
  A name that is not a field of the class is refused, naming the class's fields.
- **B3.** The value is read by the field's declared type:
  `bool` from `true` or `false`;
  `int` from an optional `-` and decimal digits;
  a `Literal` as one of its members;
  `str` verbatim.
  A field typed `X | None` takes the rules of `X`;
  it is unset by leaving it out, and no text sets it to `None`.
- **B4.** A field the text leaves out keeps the class's default.
  An unset optional field is not sent:
  the vendor's own behavior applies, as for OpenAI's `reasoning_effort` today.
- **B5.** `+passthrough` sets the recipe's `passthrough`.
- **B6.** The class then validates the combination, as its `__post_init__` does today.
- **B7.** The result must be admitted (§3.5).

A refusal raises `RecipeError` naming the text, the provider and the offending part:
an uninstalled provider names the installed ones and says to install the plug-in that provides it.

### 3.3 Framework flags

`passthrough` (#1143) is the one framework flag.
It admits a model the provider does not list and makes every MIME type accepted (#1143, R2).
It is written `+passthrough` right after the model, so it reads as a property of the model and never as a provider field:
`openai:gpt-7+passthrough,reasoning_effort=high`.
It is stored as the base class's keyword-only field, which no provider may declare.

### 3.4 Canonical text and records

`format_recipe(recipe)` writes the canonical text:
`provider:model`, then `+passthrough` when set, then `,field=value` for each field whose value differs from the class's default, in the order the class declares its fields, with `bool` written `true` or `false` and `int` in decimal.
For every recipe, `bind_recipe(format_recipe(recipe)) == recipe`.

`LLMSpec.dump()` is defined once in the base class:
`{"type": <provider>, "model": <model>, <field>: <value> for every field}`, plus `"passthrough": true` when set.
`LLMSpec.from_dict(data)` builds the named provider's class from those keys and refuses any other key.
Providers no longer implement `_dump_impl` and `_from_dict_impl`.
The dumps of OpenAI, DeepSeek and echo recipes keep their keys;
an Anthropic dump always carries a model, since the class no longer takes `None` (§3.6).

### 3.5 Recipe classes and their tables

A provider's declaration module exports `LLMSpec`, `FAILURE_SIGNATURES`, `price`, `PRICE_TABLE`, `PRICE_TABLE_SHA256`, `price_table_from_content` where it persists tables, and `MODEL_SLUG` with `model_family` where it has them.
It no longer exports `DEFAULT_MODEL`, `MODELS` or `MODEL_ID`.

The recipe class declares `TYPE`, `WIRE`, `ENDPOINT` and `MODEL_INPUTS` as today, `model: str` with no default as its first field, and its provider fields, each with a default.
The base class no longer declares `selection_fields`, `deferred_fields`, `effort_values`, `selection()`, `with_selection()`, `fast()` or `with_effort()`, and `EFFORT_LEVELS` leaves core.

- **R1.** A recipe is admitted when it carries `passthrough` or its model is a key of `MODEL_INPUTS`.
  `providers.require_admitted` checks it at `bind_recipe` and at `wires.open`, the two points #1143 enforces it at, with `bind_recipe` in place of `providers.resolve`.
- **R2.** Every listed model has a row in the provider's price table.
  The price table may also hold models that are no longer listed, so that recorded trails of a retired model keep their cost.
  Loading a declaration refuses a listed model without a price row, as it refuses a malformed table.

The recipe classes after the change:

| provider | wire | fields, with their defaults | listed models |
|---|---|---|---|
| `openai` | `responses` | `reasoning_effort`: `none`, `minimal`, `low`, `medium`, `high`, `xhigh` or `max`, default unset; `service_tier`: `auto`, `default`, `flex` or `priority`, default unset; `compact_threshold`: positive `int`, default unset | `gpt-6.1-sol`, `gpt-6-luna`, `gpt-6-astra`, `gpt-5.6-terra`, `gpt-5.6-luna` |
| `deepseek` | `chat` | `reasoning_effort`: `none`, `low`, `high` or `max`, default unset | `deepseek-v4-pro`, `deepseek-flash` |
| `anthropic` | `messages` | `effort`: `low`, `medium`, `high`, `xhigh` or `max`, default unset; `fast_mode`: `bool`, default `false` | the eight Claude models #1143 lists |
| `echo` | `echo` | none | `echo`, priced at zero |

DeepSeek's mapping of neutral effort levels onto its own scale goes with the neutral levels:
`reasoning_effort` takes DeepSeek's own values.
OpenAI's fast mode is `service_tier=priority`, and Anthropic's is `fast_mode=true`.

### 3.6 Harnesses

A harness serves the providers its `llm_providers` names:
`bro` the providers with an installed wire client (`openai`, `deepseek`, `echo`), `claude` only `anthropic`.
`Harness.check_recipe(recipe)` replaces `Harness.resolve_recipe(base, selection)`:
it refuses a recipe whose provider the harness does not serve, naming the installed harnesses that serve it, as today's refusal does.
A recipe never switches the harness.

There is no deferred model:
the Anthropic class's `model` is a required `str`, `_SESSION_DEFAULT` goes, and a Claude session always receives `--model` (§7).

## 4. Projects

### 4.1 The installation's project

- **P1.** Exactly one installed distribution declares the installation's project, through an entry point of the group `bro.project`.
  The entry's name is the project's name.
  Its value names the package holding the project settings as the resource `bro-project.toml`.
- **P2.** `bro-project.toml` is one layer in the schema of §5.1, written in TOML.
  It is package data, so it is installed with the distribution and carried by every runtime bundle frozen from the installation:
  a frozen session reads it without any checkout.
- **P3.** No `bro.project` entry, or several, refuses every launch from the installation, naming the distributions that declare one.
- **P4.** The project settings are read once when a root starts, with the rest of its reading (§6.4);
  in-process `bro run` and `bro chat` read them from the live installation.

In this repository the declaring distribution is `bro-local`, which no published wheel includes,
so a consumer that installs the framework never inherits this repository's settings.
Its `local/bro/local/bro-project.toml` takes the keys the root `[tool.bro]` holds today:

```toml
default = "bro-dev"
harness = "claude"
summon-harness = "bro"
```

A consumer repository declares its own project the same way, from its own distribution.

### 4.2 Home projects

- **H1.** Every distribution that registers bros (the `bro` entry-point group) declares its project through exactly one entry of the group `bro.home`.
  The entry's name is the project's name;
  its value names the distribution's persona package and is never loaded.
- **H2.** A bro's home project is the `bro.home` name of the distribution that registers it.
- **H3.** Reading the registry refuses a distribution that registers bros with no `bro.home` entry or with several, as it refuses two distributions claiming one bro name.

In this repository `bro`, `bro-browser`, `bro-dev`, `bro-local` and `bro-oops` each declare the home `bro`.
A consumer's own distribution declares its project, so `ppp-dev`, subclassing `bro-dev` in ppp, has the home `ppp`.

## 5. Configuration

### 5.1 Layers and their keys

One schema serves every layer;
the table says which keys each level takes.

| key | value | project settings | `defaults` | `projects.<project>` | `projects.<project>.bros.<bro>` |
|---|---|---|---|---|---|
| `default` | the project's default bro | yes | — | — | — |
| `harness` | a harness name, for a root launch | yes | yes | yes | yes |
| `summon-harness` | a harness name, for a summon | yes | yes | yes | yes |
| `summon-depth` | a positive `int` | yes | yes | yes | yes |
| `llm` | an object from harness name to recipe text | yes | yes | yes | yes |
| `grant`, `revoke`, `limit` | lists in the authority syntax | yes | yes | yes | yes |
| `creds` | `kind+instance` picks | — | — | yes | yes |
| `bros` | an object from bro name to bro layer | — | — | yes | — |

- **K1.** `default` names the bro that `dive-in` and `ride scope` take when none is named;
  only a project sets it.
- **K2.** `creds` selects stored credential instances, which only the host may name, as today.
- **K3.** `llm` maps each harness to the recipe text the bro runs on it:
  `{"claude": "anthropic:claude-opus-5-5,effort=xhigh", "bro": "openai:gpt-5.6-terra,reasoning_effort=medium"}`.
- **K4.** `~/.bro.json`'s top level takes `defaults`, `projects`, `user` and `operator`.
  `user` and `operator` keep their meaning.
  The top-level `harness`, `summon-harness`, `summon-depth` and `llm` go:
  the first three move into `defaults`, and `llm` (presets) leaves.
- **K5.** A key of `projects` is a project name.
  Path and URL keys go, and the attached repository plays no part in configuration.
- **K6.** `summon-depth` bounds the summon chain under a root, and the root bro's stack supplies it;
  a summoned bro's layers do not change the bound of the chain it joins.

### 5.2 The layer stack

For a bro `B` whose home project is `H`, launched from an installation whose project is `P`, the layer stack is, least specific first:

1. `P`'s project settings;
2. `defaults`;
3. `projects.P`;
4. `projects.H.bros.B`, when `H` differs from `P`;
5. `projects.P.bros.B`.

The launch's own choices then apply over the stack.

- **L1.** For `default`, `harness`, `summon-harness` and `summon-depth`, the last layer setting the key wins.
- **L2.** For `llm`, the last layer whose map has the harness as a key wins, harness by harness:
  a bro layer that names only `bro` leaves the `claude` recipe to the layers before it.
- **L3.** `creds` keeps today's rule:
  per kind, the last layer picking it wins over the store's `creds.json` defaults.
- **L4.** `grant`, `revoke` and `limit` fold in stack order, each layer applying its caps and then its grants, as the configuration fold does today (`bro/reference/permissions.md`, "Readings, bases and configuration").
- **L5.** A summon's stack is the child bro's, in the root's reading:
  the child's own home and project entries, not the summoner's.
- **L6.** A layer the host configuration does not have contributes nothing:
  a host with no `~/.bro.json` runs on the project settings alone.

The project settings are the least specific layer, so every layer the host writes overrides what a project ships.

### 5.3 Harness selection

A root launch runs on `--harness` when given, otherwise on the stack's `harness`;
a summon runs on its `harness` field when given, otherwise on the child's stack's `summon-harness`.
With neither, the start is refused:
`no harness for <bro>: pass --harness, or set "harness" in a layer of ~/.bro.json or in the project settings`, with `summon-harness` and `harness` for a summon.
The selected name must be an installed harness.
`ride resume` keeps the recorded harness.

### 5.4 What a launch reads and validates

At a root's start, the reading validates the parts of the configuration the installation owns, strictly and in full:

- **V1.** The parts are:
  the project settings;
  `~/.bro.json`'s top level;
  `defaults`;
  `projects.P`, every bro layer in it included;
  and `projects.H.bros.B` for every installed bro `B` whose home `H` differs from `P`.
- **V2.** Nothing else in `projects` is read, so another project's mistakes cannot reach this installation's launches.
- **V3.** In every part read, an unknown key is refused, and so is a value of the wrong shape.
  A credential kind and a harness name must be well formed.
- **V4.** A recipe under a harness this installation has is bound in full (§3.2):
  its provider must be installed, its fields valid and its model admitted.
  A recipe under a harness this installation lacks is checked by `parse_recipe` alone, since another installation reading the same file may run it.
- **V5.** A failure refuses every launch from the installation, naming the layer path and the key:
  `~/.bro.json: projects.bro.bros.eyebro.llm.bro: unknown field 'effort' for openai; fields: reasoning_effort, service_tier, compact_threshold`.

Bro and credential names are resolved against the installation as today:
an unknown bro under `projects.P.bros` is refused, and so is an unregistered credential kind in a part the installation reads.

### 5.5 The file

```json
{
  "defaults": {
    "harness": "claude",
    "summon-harness": "bro",
    "llm": {
      "claude": "anthropic:claude-opus-5-5,effort=xhigh",
      "bro": "openai:gpt-5.6-terra,reasoning_effort=medium"
    },
    "grant": ["bro.placement=join"]
  },
  "projects": {
    "bro": {
      "creds": ["brog+github", "github+dev"],
      "bros": {
        "bro-dev": {"llm": {"bro": "openai:gpt-6.1-sol,reasoning_effort=high"}},
        "eyebro": {"llm": {"bro": "openai:gpt-6.1-sol,reasoning_effort=xhigh", "claude": "anthropic:claude-opus-5-5,effort=max"}},
        "terminal": {"llm": {"bro": "openai:gpt-5.6-terra,compact_threshold=200000"}},
        "browser": {"creds": ["cookies+assistant"]}
      }
    },
    "ppp": {
      "bros": {"ppp-dev": {"llm": {"claude": "anthropic:claude-opus-5-5,effort=xhigh,fast_mode=true"}}}
    }
  },
  "user": {"creds": ["github+me"]}
}
```

## 6. Resolution

### 6.1 The resolution function

`resolve_recipe(reading, bro, harness, explicit=None)` (`ride/ride/authority_scope.py`) returns the resolved recipe and its source.

1. When `explicit` recipe text is given, it is the text, and the source is the surface that passed it (§6.2).
   Otherwise the text is the winning `llm` entry for the harness under L2, and the source is that layer's path.
   With neither, the start is refused:
   `<bro> has no recipe for harness <harness>: pass --llm, or set llm.<harness> in a layer of ~/.bro.json or in the project settings`.
2. The text is bound (§3.2).
3. The harness must serve the recipe (§3.6).

Nothing is merged across sources:
an explicit text replaces the configured one whole, and two layers never combine into one recipe.

### 6.2 Surfaces

| surface | explicit text | harness | reading | source when explicit |
|---|---|---|---|---|
| `ride solo`, `ride along` | `--llm` | `--harness`, else the stack | the root's | `--llm` |
| `dive-in` | `--llm`, forwarded | as `ride along` | as `ride along` | `--llm` |
| a summon (tool, `summon` CLI) | `llm` | `harness`, else the child's stack | the root's (§6.4) | `summon llm` |
| a manual summon's commit | the operator's `--llm` | the operator's `--harness`, else the child's stack | the root's | `--llm` |
| `ride resume` | `--llm`, else the recorded recipe (§8) | the recorded harness | a fresh reading | `resume --llm`, or the recorded source |
| `ride scope` | `--llm` | `--harness`, else the stack | a fresh reading | `--llm` |
| `bro run`, `bro chat` | `--llm` | `bro` | the live installation's | `--llm` |
| `bro chat --fork` | as `bro chat` | `bro` | as `bro chat` | `--llm` |

- **S1.** `ride scope` prints the resolved recipe's canonical text and source, or the refusal a launch would meet;
  it no longer needs `--repo` to see configuration, since configuration follows the installation.
- **S2.** `bro run` and `bro chat` resolve through the same function and stack as a managed launch, on the bro harness.
  They read only the stack's `llm`; their credentials stay ambient, as today.
  The managed native session passes `--resolved-llm`, the dump of the recipe its launcher resolved, which `bro run` and `bro chat` take verbatim and still check at `wires.open`.
- **S3.** `bro.fork.fork` called without a recipe rebuilds the trail header's, as today, and `wires.open` admits it.
- **S4.** `bro.native.llm`, the provider diagnostic, takes a required `--llm` and reads no configuration.
- **S5.** `mu` binds its material's recipe texts (`llm` and each role's) with `bind_recipe`, with no layers and no harness;
  the recipe's wire must have an installed client.
- **S6.** The benchmark's Harbor `model_name` is `<provider>/<rest of the recipe text>`;
  `harbor_agent.llm_recipe` joins the two parts with `:` and checks the result with `parse_recipe`, and the trial passes it to `ride solo --llm`.

### 6.3 Authority

The bro worker type's LLM options are `provider` (a provider name), `model` (a string) and `passthrough` (a flag), all open, none overridable.
`effort`, `fast` and the `~default` symbol go, and so does `bro/authority`'s `Symbol` value, which only `~default` used.

- **A1.** A start's request carries the explicit recipe text, if any, as ungoverned content under `llm`.
- **A2.** The descriptor's `provider`, `model` and `passthrough` are the resolved recipe's, whether it came from the request or from configuration, so a cap on any of them refuses the start, a configured default included, and never changes it.
- **A3.** Admission recomputes the resolved recipe and refuses a start whose descriptor names a different provider, model or `passthrough`.

### 6.4 When configuration is read

A root reads the installation's project settings and `~/.bro.json` once, at its start.
Its summons resolve against that reading, so an edit made later reaches only roots started after it;
`ride override` remains the way to change a running root's authority.

## 7. Into the session

The start carries the resolved recipe as `resolved_llm`, and the session receives its dump in `RIDE_RESOLVED_LLM` and its source in `RIDE_RESOLVED_LLM_SOURCE`.

- The native harness passes `--resolved-llm` to `bro run` or `bro chat`, which uses the recipe verbatim.
- The Claude harness passes `--model <model>` always, `--effort <effort>` when the recipe sets `effort`, and `"fastMode": <fast_mode>` in its `--settings`.
  A recipe without `effort` leaves effort to Claude Code, as an unset field leaves it to the vendor (B4).
- The Claude recorder records the recipe in the trail header, as today;
  each call records the model that answered.

## 8. Resume

- **E1.** A session's resume record keeps its resolved recipe's dump and source.
- **E2.** `ride resume` runs the recorded recipe unless `--llm` replaces it;
  a replacement is resolved as on `ride along`, on the recorded harness.
- **E3.** A recorded recipe whose model has since been delisted is refused at binding;
  `ride resume --llm` with a listed model, or the same model with `+passthrough`, resumes it.
- **E4.** A record without a resolved recipe, written before this design, is refused, naming the workspace and the two ways on:
  `bro chat <bro> --fork <trail>` for a native session, or a new session.

## 9. What the session and the tools show

- The banner (`bro::banner`, `ride banner`) gains the line `llm: <canonical text> (<source>)`.
- `ride scope` prints the same, or the refusal (S1).
- `bro show` shows no recipe and no LLM-key line, since a persona declares none.

## 10. Failure behavior

| condition | raised by | message names | before |
|---|---|---|---|
| recipe text off the grammar, a repeated flag or field, an unknown flag | `parse_recipe` | the text and the part | any provider is loaded |
| an uninstalled provider | `bind_recipe` | the provider, the installed ones | the start |
| an unknown field, a value of the wrong type, an invalid combination | `bind_recipe` | the field, the class's fields or the value's type | the start |
| an unlisted model without `+passthrough` | `bind_recipe`, `wires.open` | the model, the listed ones, `+passthrough` | the start, or the request |
| a recipe the harness does not serve | `Harness.check_recipe` | the harness, the harnesses that serve it | the start |
| no recipe for the harness | `resolve_recipe` | the bro, the harness, where to set one | the start |
| no harness | §5.3 | the bro, where to set one | the start |
| a malformed part of the configuration the installation owns | the reading (§5.4) | the layer path and key | every launch |
| no `bro.project`, or several | the reading (P3) | the declaring distributions | every launch |
| a bro-registering distribution without exactly one `bro.home` | the registry (H3) | the distribution | the registry is read |
| a listed model without a price row | `providers.declaration` (R2) | the model | the provider is used |
| a descriptor that differs from the resolved recipe | admission (A3) | the option | the start |
| a resume record without a resolved recipe | `ride resume` (E4) | the workspace and the ways on | the resume |

## 11. What goes

| mechanism | replaced by |
|---|---|
| `LLMSelection`, `over`, `parse`, `parse_with_presets`, `is_preset`, `resolve`, `resolve_model`, `default_spec`, `carriers`, `provider_of_model` | `parse_recipe`, `bind_recipe`, `format_recipe` (§3) |
| `LLMSelectionError` | `RecipeError`, raised by every refusal of §3 |
| `--provider`, `--model`, `--effort`, `--fast`, `--passthrough` | `--llm` with complete recipe text |
| `[tool.bro.llm]` and the host's top-level `llm` presets | nothing, until presets return |
| `MODELS`, `DEFAULT_MODEL`, `MODEL_ID` | full model ids in recipe text, admitted by `MODEL_INPUTS` |
| `BaseBro.llm_spec` and the five personas' declarations | `llm` in configuration; the run's recipe passed to the runner explicitly |
| `_SESSION_DEFAULT`, the Anthropic class's `model=None`, `deferred_fields`, `default` in host lists, `~default` | a model in every recipe |
| `EFFORT_LEVELS`, `with_effort`, `fast`, `effort_values`, `selection_fields`, `with_selection` | the provider's own fields |
| the host's ordered `llm` lists and first-served selection | `llm` maps keyed by harness (K3, L2) |
| the authority options `effort` and `fast` | none, until authority over the whole recipe |
| project keys by path and URL, and `[tool.bro]` read from the attached repository for anything but the tree | project names and the installation's project settings (§4.1, K5) |
| the host's top-level `harness`, `summon-harness`, `summon-depth` | the same keys in layers (§5.1) |
| `Harness.resolve_recipe`, `Harness.runs`, `SessionHarness.resolve_llm`, the reading's per-bro base recipes | `Harness.check_recipe`, `resolve_recipe` (§6.1) |
| `canonicalize`, `drop_piece_flags`, `host_config.launch_llm`, `LLMDefault` | nothing: they have no caller outside tests |

## 12. Records, other processes and rollout

### 12.1 Records

- Trail headers keep recording `dump()`.
  Recorded headers stay readable:
  `rewind`, cost and the benchmark read their fields as stored, and only `bro.fork.fork` rebuilds a header's recipe, from native trails, whose recipes always name a model.
  An Anthropic header recorded with `model: null` is never rebuilt.
- Resume records gain the resolved recipe and its source (E1);
  older ones are refused (E4).
- Benchmark retention keeps the trial's `model_name` as written.

### 12.2 Contracts with other processes

- **`~/.bro.json`** is read by every installation on the host.
  An installation from before this design refuses the new schema, and this design's refuses the old one.
  So between the host's re-sync and the file's rewrite, the host's installation cannot launch;
  ppp's and kap's installations cannot launch from the rewrite until they move their framework pin past this design and declare their `bro.project` and `bro.home` entries.
- **`mu` material** is hydrated into a session's store at its start and parsed at each call.
  A session started before the rewrite fails its `mu` calls (cast, summaries, `look`) once the material is rewritten.
- **The benchmark bundle** carries its own runtime, which parses the `--llm` its trial passes;
  it is rebuilt after the re-sync, and the agent presets' `model_name`s change in the same landing.
- **The summon payload** is read only by processes of the root's runtime bundle, so its `llm`-only shape needs no order.
- **The hosted trails server** decodes responses and reads trail bodies;
  it parses no recipe text, so it is not redeployed.

### 12.3 Rollout order

1. The landing merges.
2. The interactive configuration step (§12.4) writes `~/.bro.json.next` and the new `mu` material beside the current files.
3. Every root on the host ends.
4. The host's installation re-syncs to the landed commit, the two new files replace the current ones with backups kept, and the benchmark bundle is rebuilt.
5. Fresh roots start.
6. ppp and kap move their framework pins and declare their projects, as their own work.

Rollback restores the two backups and re-syncs the previous commit.

### 12.4 The interactive configuration step

The last step of the implementation plan is attended and needs the host's own files, so the user launches it as a manual, unboxed session before the re-sync, on the runtime the host still runs.

1. It reads what each installed bro does, in this installation and in ppp's and kap's:
   its persona prompt, tools and spells (`bro show`), and the bros it summons.
2. It reads the current `~/.bro.json` and `mu` material.
3. It proposes the new file:
   `defaults` with a recipe per harness;
   for each project, its credential picks and grants carried over from the URL and path entries they sit under today;
   and, for each bro whose work calls for a different model or knob, a bro layer with the recipe and the reason.
   It proposes the `mu` material's recipes in the new syntax.
4. The user settles every entry.
5. It writes `~/.bro.json.next` and the new `mu` material beside the current files, and validates them with the landed code:
   `uv run -q ride scope --bro <bro> --harness <harness>` for every installed bro and harness, run from the landed checkout under a scratch `HOME` that holds the candidate files.

## 13. Examples

The examples read the file of §5.5, from this repository's installation (`P` = `bro`), unless they say otherwise.

1. `ride along bro-dev` with no `--harness`:
   the stack's `harness` is `claude`, from `defaults`;
   the only `claude` entry is in `defaults`, so the recipe is `anthropic:claude-opus-5-5,effort=xhigh` from `defaults`;
   Claude Code starts with `--model claude-opus-5-5 --effort xhigh` and `fastMode` false.
2. A summon of `bro-dev` with no `harness`:
   the child's `summon-harness` is `bro`, from `defaults`;
   `projects.bro.bros.bro-dev.llm.bro` wins over `defaults.llm.bro` (L2), giving `openai:gpt-6.1-sol,reasoning_effort=high`.
3. A summon of `bro-dev` with `llm: "anthropic:claude-opus-5-5"` and no `harness`:
   the harness is `bro`, which does not serve `anthropic`, so the summon is refused, naming the `claude` harness.
4. `ride along --llm openai:sol eyebro --harness bro`:
   binding refuses `sol`, which is not a listed `openai` model, naming the five listed ones and `+passthrough`.
5. `ride along --llm openai:gpt-7+passthrough,reasoning_effort=high eyebro --harness bro`:
   admitted by `passthrough`;
   the banner shows `llm: openai:gpt-7+passthrough,reasoning_effort=high (--llm)`.
6. `terminal` summoned on `bro`:
   `openai:gpt-5.6-terra,compact_threshold=200000`, whose unset `reasoning_effort` is left to OpenAI.
7. From ppp's installation (`P` = `ppp`), a summon of `browser`, whose home is `bro`:
   the stack is ppp's project settings, `defaults`, `projects.ppp`, `projects.bro.bros.browser`, then `projects.ppp.bros.browser`, which the file lacks;
   `browser`'s `creds` come from `projects.bro.bros.browser`, and its `bro` recipe from `defaults`.
8. A typo under `projects.ppp`:
   this repository's installation never reads `projects.ppp`, so its launches proceed;
   every launch from ppp's installation is refused, naming the layer path and key.
9. `projects.bro.bros.eyebro.llm.bro` = `openai:gpt-6.1-sol,effort=xhigh`:
   every launch from this repository's installation is refused (V5), since `effort` is not an OpenAI field.
10. `ride resume <workspace>` of a session that ran `openai:gpt-6.1-sol,reasoning_effort=high`:
    it runs the same recipe, whatever the configuration now says;
    `ride resume --llm openai:gpt-6.1-sol,reasoning_effort=max <workspace>` runs that instead.
11. `bro run dev 'hi'`:
    `bro run` resolves on the `bro` harness through the same stack, so `dev` runs `openai:gpt-5.6-terra,reasoning_effort=medium` from `defaults`.
12. Recipe texts:
    `openai:gpt-5.6-terra` binds with every field at its default, and its canonical text is the same;
    `openai:gpt-5.6-terra,service_tier=priority,reasoning_effort=high` binds, and its canonical text is `openai:gpt-5.6-terra,reasoning_effort=high,service_tier=priority`;
    `anthropic:claude-opus-5-5,fast_mode=yes` is refused, since `fast_mode` is a `bool`;
    `openai:gpt-5.6-terra,reasoning_effort=high,reasoning_effort=low` is refused (T2);
    `openai:gpt-5.6-terra+fast` is refused (T3).

## 14. Decision record

The `## Step 2 outcome` of #1150 records the decisions D1 to D7, S1 to S3, C1 to C5 and L1 to L5, all settled by the owner on 2026-10-31;
this design states them as the rules above.
The decisions below were raised by the design.

| id | decision | status |
|---|---|---|
| N1 | every listed model is priced, while the price table may keep retired models for recorded trails; echo is priced at zero (R2) — amends C5, whose "same set" would drop the cost of recorded trails or relist retired models | proposed |
| N2 | the installation's project is declared by one `bro.project` entry point naming a package that holds `bro-project.toml` as package data (§4.1); in this repository, `bro-local` declares it | proposed |
| N3 | a bro's home is declared by one `bro.home` entry point per registering distribution, and the registry refuses a distribution without exactly one (§4.2) | proposed |
| N4 | `harness`, `summon-harness` and `summon-depth` are keys of every layer, and the host's top-level copies go (§5.1, K4) | proposed |
| N5 | the project settings are the least specific layer, below `defaults` (§5.2), so the host's choices override what a project ships; today a project's `harness` beats the host's | proposed |
| N6 | the project settings may carry `llm`, so a project ships recipes for its harnesses; bro layers are the host's only (§5.1) | proposed |
| N7 | path and URL project keys go, and with them configuration that differs between machines sharing one `~/.bro.json` (K5) | proposed |
| N8 | the recipe grammar has no whitespace and no `+` or `,` in a model id; fields are typed by their declarations and unset by omission; the canonical text omits default values (§3.1 to §3.4) | proposed |
| N9 | `dump` and `from_dict` are defined once over the recipe class's fields (§3.4) | proposed |
| N10 | the resume record keeps the resolved recipe and its source, `ride resume` takes `--llm`, and older records are refused (§8) | proposed |
| N11 | the banner shows the source beside the recipe, and the session receives it in `RIDE_RESOLVED_LLM_SOURCE` (§7, §9) | proposed |
| N12 | `passthrough` stays an open authority option (§6.3) | proposed |
| N13 | `bro.native.llm` takes a required `--llm` (S4) | proposed |
| N14 | recipes under a harness the installation lacks are checked for grammar only (V4) | proposed |
| N15 | `bro run` and `bro chat` resolve through the managed stack (S2) | proposed |
| N16 | the interactive configuration step runs on the pre-rollout runtime and writes beside the current files, validated with the landed code (§12.4) | proposed |

## 15. Implementation mapping

- `bro/llm/llm.py` — the base class of §3.5: the required `model`, generic `dump` and `from_dict`, `admitted` without the deferred clause;
  `EFFORT_LEVELS`, `selection_fields`, `deferred_fields`, `effort_values`, `selection`, `with_selection`, `fast` and `with_effort` go.
- `bro/llm/providers.py` — `parse_recipe`, `bind_recipe`, `format_recipe`, `require_admitted`; declaration validation without `DEFAULT_MODEL` and `MODELS`, with R2; the mechanisms of §11's first row go.
- `llms/openai`, `llms/deepseek`, `llms/anthropic`, `harnesses/native/harness/bro/native/echo_declaration.py` — explicit `MODEL_INPUTS` keys, the fields of §3.5, no `_dump_impl` or `_from_dict_impl`; echo's price row.
- `bro/harness/__init__.py`, `ride/ride/harness.py` — `check_recipe`; `resolve_recipe(base, selection)`, `runs` and `resolve_llm` go.
- `bro/bro.py`, `bro/registry.py` — `llm_spec` leaves the declaration; `create_bro` and `missing_secrets` take the run's recipe; the registry reads `bro.home` (H1 to H3) and the installation's `bro.project` (P1 to P3).
- `bro/show.py` — no recipe line.
- `bro/launch/llm_flags.py` — one `--llm` flag; presets and the piece flags go.
- `bro/base/host_config.py` — the schema of §5.1 and the validation of §5.4.
- `bro/workspace/project.py` — `[tool.bro]` keeps only the tree's keys, for #1157 to take over;
  the default `image-repository`, `bro/<default bro>`, takes the default bro from the project settings;
  the project settings reader.
- `ride/ride/authority_reading.py` — the reading of the project settings, the layer stack (§5.2), the validated parts (§5.4); per-bro base recipes go.
- `ride/ride/authority_scope.py` — `resolve_recipe` (§6.1), the options of §6.3, `checked_recipe` built on them.
- `ride/ride/cli.py`, `flags.py`, `session.py`, `scope.py`, `scope_report.py`, `dive_in.py`, `bro_worker.py`, `operator.py`, `launch_control.py` — `--llm` alone;
  the resume record and `ride resume --llm` (§8); scope's output (S1); the stack in place of attachment identities.
- `bro/summon.py` — the payload's `llm` alone.
- `bro/workspace/banner.py`, `ride/ride/do_ride.py` — the banner line and `RIDE_RESOLVED_LLM_SOURCE`.
- `harnesses/native/harness/bro/launch/run.py`, `call.py`, `native/llm.py`, `native/runner.py`, `native/harness.py`, `fork.py` — S2 to S4; the runner takes the run's recipe explicitly.
- `harnesses/claude/harness/bro/claude/harness.py`, `claude_argv.py` — §7.
- `bro/llm/mu.py` — S5.
- `benchmark/bro/benchmark/harbor_agent.py`, `trial_store.py`, `retention.py`, `trajectory.py`, `benchmark/agents/*.json` — S6, the presets in recipe text, and the class's `WIRE` read from the declaration in place of `default_spec`.
- `bro/authority/values.py`, `syntax.py`, `records.py` — `Symbol` and `~` go.
- The five personas (`browser`, `terminal`, `devoops`, `bro-dev`, `bro-eyebro`) — their `llm_spec` goes, and with it each distribution's dependency on a provider plug-in that nothing else uses.
- Every distribution registering bros — its `bro.home` entry; `bro-local` — its `bro.project` entry and `bro-project.toml`; the root `pyproject.toml` — `default`, `harness` and `summon-harness` leave `[tool.bro]`.
- A policy test (`local/bro/local/`) — every installed distribution registering bros declares one `bro.home`.

## 16. Documentation

- `bro/reference/ride.md` — "Harness selection", "Recipe selection", "Per-project defaults", the lifecycle verbs (`ride resume --llm`, `ride scope`), the banner, `RIDE_RESOLVED_LLM_SOURCE`.
- `bro/setup/AGENTS.md`, "Host config", and `bro/base/host_config.py`'s docstring — the schema of §5.
- `bro/reference/llm_providers.md` — the declaration contract (§3.5), recipe text, admission without deferred models.
- `bro/reference/extending.md` — declaring a bro without a recipe; `bro.home` and `bro.project`.
- `bro/reference/permissions.md` and `permissions_cheatsheet.md` — the bro option table (§6.3).
- `bro/reference/mu.md` and `permissions.md`, "Auxiliary LLM credentials" — `mu` material in recipe text.
- `bro/llm/AGENTS.md`, `bro/launch/AGENTS.md`, `harnesses/native/harness/AGENTS.md`, `bro/AGENTS.md`, `benchmark/AGENTS.md` — the entries naming removed symbols.
- `README.md`, `BOOTSTRAP.md`, `bros/bro/spells/ask.md`, `dev/bros/lead/spells/orchestrate.md` — `--llm` in recipe text, the project declaration for a consumer, and the launch lines that pass `--effort`.
- Any other page naming a removed symbol, found by grep when the change is made.

## 17. Unverified assumptions

- **X1.** Claude Code 2.1.296 starts under the setup token with `--model` set to each of the eight listed Anthropic ids.
- **X2.** Harbor 0.24.0 carries a `model_name` containing `,` and `=` unchanged:
  read in its source (`harbor/agents/base.py`, `harbor/models/job/result.py`), not run.
- **X3.** The owner's `~/.bro.json` holds no configuration that differs between the machines sharing it through a path key (N7).
- **X4.** A package-data resource reaches every installation mode the framework supports, including the wheels a runtime bundle builds from local distributions.
