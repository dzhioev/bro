# Design: model selection — complete recipes from configuration, one resolution

This file is the specification of the refactor that [#1150](https://github.com/dzhioev/bro/issues/1150) carries.
It lives on the branch `design/1150-model-selection`, which is never merged:
the pull request that carries it is a design review only.
It starts from the decisions recorded under `## Step 2 outcome` in #1150's description, now in its edit history, from the discussion that followed them, and from #1143 as it landed (`5431fdd2`);
the decision record (§14) lists every decision with its status.

## 1. Goal, scope and constraints

Goal: anyone can say which model a session runs, and why.
A recipe is always complete and written in one syntax;
it comes either from configuration or from an explicit `--llm`;
one function resolves it on every surface;
and the session shows the recipe and the configuration entry it came from.

What it enables, for example:

- In this repository's installation, `ride along --harness bro bro-dev` runs `openai:gpt-6.1-sol,reasoning_effort=high`, because `homes.bro.bros.bro-dev.llm.bro` in `~/.bro.json` names it,
  and the banner says `llm: openai:gpt-6.1-sol,reasoning_effort=high (homes.bro.bros.bro-dev)`.
- The same bro on the Claude harness runs `anthropic:claude-opus-5-5,effort=xhigh` from `defaults.llm.claude`,
  and Claude Code is started with `--model claude-opus-5-5 --effort xhigh`.
- `browser`, summoned from ppp's installation, reads its credentials and recipes from `homes.bro.bros.browser`, its home's entry, unless `homes.ppp.bros.browser` overrides them.
- `ride along --harness bro --llm openai:gpt-6.1-sol,reasoning_effort=max eyebro` runs exactly that recipe;
  `--llm :sol` and `--llm openai` are refused as incomplete.
- `terminal` keeps its context bound through configuration: `openai:gpt-5.6-terra,compact_threshold=200000`.
- A malformed entry under `homes.ppp` cannot stop a launch from this repository's installation.

In scope:
the recipe representation (syntax, fields, flags, records) and the provider declarations' tables;
homes: how a bro belongs to one, how a home ships settings, and how an installation names its own;
the host configuration's schema, layering and validation;
the one resolution function and every surface that calls it;
the summon-depth allowance;
the LLM authority options;
how the resolved recipe reaches each harness and what the session shows of it;
which keys the attached tree's `[tool.bro]` still holds, and the name of the image a boxed session on it runs in (§5.7);
the removal of the mechanisms §11 lists;
records, migration and rollout, ending in an interactive step that writes the new `~/.bro.json`.

Out of scope, each with where it belongs:

- repository attachment (`--repo`, URL mirrors, path and URL bases, detached sessions as the base mode)
  and the keys describing the attached tree that §5.7 leaves in place (`build-context-command`, `setup.sh`, `[tool.bro.devoops]`)
  ([#1157](https://github.com/dzhioev/bro/issues/1157));
- authority over the whole recipe, a tool listing the recipes a summoner may pass to a bro, and LLM hints (follow-ups recorded on #1150);
- `--llm` presets, which leave now and return later in another shape;
- restricting model choice inside an interactive Claude session (`/model`), which stays free;
- offering an agent tools according to what its model accepts ([#1046](https://github.com/dzhioev/bro/issues/1046), [#1047](https://github.com/dzhioev/bro/issues/1047)).

Constraints:
core imports no other workspace member, and a provider declaration imports no SDK at module level (`bro/llm/AGENTS.md`);
code names a provider only in a distribution that depends on the plug-in providing it (`local/bro/local/provider_policy_test.py`);
recorded trails stay readable;
backward compatibility is not a constraint (`AGENTS.md`, "Conventions"), and other installations that read the same `~/.bro.json` can neither launch nor read a credential until they move past this design (§12.2).

## 2. Concepts

- **Provider** — a vendor's model API as one plug-in declares it (`openai`, `deepseek`, `anthropic`, `echo`; `bro/llm/providers.py`).
- **Recipe** — a complete choice of how to call a model: a provider, a model id, the framework flags (§3.3) and the provider's fields (§3.2).
  In code it is an instance of the provider's recipe class, a subclass of `Recipe` (`bro/llm/llm.py`).
- **Field** — one knob of a provider's recipe class other than the model and the flags, under the provider's own name: OpenAI's `reasoning_effort`, Anthropic's `effort`.
- **Framework flag** — a property of a recipe that the framework itself interprets and no provider declares; `passthrough` is the only one (§3.3).
- **Recipe text** — a recipe written as a string in the grammar of §3.1, the one form a recipe takes on the command line and in every configuration file.
  **Canonical text** is the one recipe text `format_recipe` writes for a recipe (§3.4).
- **Listed model** — a key of the recipe class's `MODEL_INPUTS` table (#1143);
  a recipe is **admitted** when its model is listed or it carries `passthrough` (§3.5).
- **Retired model** — a model a provider once admitted and no longer does, kept with its retirement date so recorded trails stay priced (§3.5).
- **Harness** — the engine a session runs on (`bro`, `claude`);
  a harness **serves** the providers named by its `llm_providers` (§3.6).
- **Distribution** — an installable Python package built from one `pyproject.toml`, such as `bro-dev` or `bro-local`.
- **Installation** — one Python environment holding distributions, together with the runtime bundles frozen from it (`~/bro/.venv`, `~/ppp/.venv`).
- **Home** — a name, `[a-z][a-z0-9-]*`, that bros belong to and that configuration is filed under:
  this repository's home is `bro`, ppp's is `ppp`.
- **A bro's home** — the home whose entry-point group registers it (§4.1): `browser`'s home is `bro`.
- **Home settings** — the settings file a home ships in one of its distributions (§4.2).
- **The installation's home** — the home an installation names in its marker file (§4.3).
  Its home settings and its section of the host configuration apply to every bro the installation launches.
- **Host configuration** — `~/.bro.json`, the host's own layers (§5).
- **Layer** — one object of configuration in the schema of §5.1: home settings, `defaults`, `homes.<home>`, or `homes.<home>.bros.<bro>`.
  A layer's **name** is its path in the host configuration, such as `homes.bro.bros.bro-dev`, or `<home> home settings` for home settings, such as `bro home settings`;
  messages and the banner name a layer this way.
- **Foreign entry** — `homes.<home>.bros.<bro>` for a bro whose home is not the installation's home (§5.4).
- **Layer stack** — the ordered layers that apply to one bro launched from one installation (§5.2).
- **Reading** — the snapshot of the installation, its home settings and the host configuration that a root takes at its start (`ride/ride/authority_reading.py`);
  its summons reuse it (§6.4).
- **Resolved recipe** — the recipe a start admits (§6.1), together with its **source**:
  the name of the layer that named it, the launch surface that passed it (§6.2), or `trail <id>` for the recorded recipe a fork runs (S3).
  One record carries both everywhere (§7): `{"recipe": <the recipe's dump>, "source": <the source>}`.
- **Allowance** — how many more generations of missions a bro may launch below itself, lowered by its **cap** (§5.6).
- **Project image** — the image a boxed session on an attached tree runs in: the runtime image with the tree's dependencies installed (§5.7).

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
A model id containing `+`, `,` or whitespace cannot be written, and no recipe holds one (R6).

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
- **B3.** The value is read by the field's declared type (R5):
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
- **B7.** The result must be admitted (R1).

A refusal raises `RecipeError` naming the text, the provider and the offending part:
an uninstalled provider names the installed ones and says to install a plug-in that provides it.

### 3.3 Framework flags

`passthrough` (#1143) is the one framework flag.
It admits a model the provider does not list and makes every MIME type accepted, as #1143 defines it.
It is written `+passthrough` right after the model, so it reads as a property of the model and never as a provider field:
`openai:gpt-7+passthrough,reasoning_effort=high`.
It is stored as the base class's keyword-only field, which no provider may declare.

### 3.4 Canonical text and records

`format_recipe(recipe)` writes the canonical text:
`provider:model`, then `+passthrough` when set, then `,field=value` for each field whose value differs from the class's default, in the order the class declares its fields, with `bool` written `true` or `false` and `int` in decimal.
Since every field is writable (R5) and every value is (R6), `bind_recipe(format_recipe(recipe)) == recipe` for every recipe.

`Recipe.dump()` is defined once in the base class:
`{"type": <provider>, "model": <model>, <field>: <value> for every field}`, plus `"passthrough": true` when set.
`Recipe.from_dict(data)` builds the named provider's class from those keys:
`type` and `model` are required, a field the data leaves out takes the class's default, and any other key is refused.
An OpenAI header recorded before 2026-07-04 has no `compact_threshold`, so it loads with the field unset.
`from_dict` checks no admission, so a record of a retired model stays readable;
the paths that run a rebuilt recipe check it (R1).
Providers no longer implement `_dump_impl` and `_from_dict_impl`.
The dumps of OpenAI, DeepSeek and echo recipes keep their keys;
an Anthropic dump always carries a model, since the class no longer takes `None` (§3.6).

### 3.5 Recipe classes and their tables

A provider's declaration module exports `Recipe`, `FAILURE_SIGNATURES`, `price`, `PRICE_TABLE`, `PRICE_TABLE_SHA256`, `price_table_from_content` where it persists tables, and `MODEL_SLUG` with `model_family` where it has them.
It no longer exports `DEFAULT_MODEL`, `MODELS`, `MODEL_ID` or Anthropic's `DEFAULT_EFFORT`.

The recipe class declares `TYPE`, `WIRE`, `ENDPOINT` and `MODEL_INPUTS` as today, and `RETIRED_MODELS` beside `MODEL_INPUTS`, so every per-model fact sits on the class;
it declares `model: str` with no default as its first field, and its provider fields, each with a default.
`needed_secrets` becomes a class method, and a harness lists the providers it serves from the declarations' classes (§3.6):
nothing builds a recipe to learn a provider's wire or LLM key, and `providers.available_specs` and `default_spec`, which build one with no arguments, go.
The base class no longer declares `selection_fields`, `deferred_fields`, `effort_values`, `selection()`, `with_selection()`, `fast()` or `with_effort()`, and `EFFORT_LEVELS` leaves core.

- **R1.** A recipe is admitted when it carries `passthrough` or its model is a key of `MODEL_INPUTS`.
  `providers.require_admitted` checks it wherever a recipe enters a run:
  `bind_recipe` for recipe text;
  `ride resume` (E3) and `bro.fork.fork` (S3) for a recipe `Recipe.from_dict` rebuilt from a record;
  and `wires.open` again for every recipe a native run opens a wire for, the managed session's `--resolved-recipe` included.
- **R2.** The class's `RETIRED_MODELS` maps each retired model id to its retirement date, the day the framework stopped admitting it.
- **R3.** The models of the price table are exactly the listed models together with the retired ones, and no model is both listed and retired.
  Cost prices each call by the model id the vendor returned, and OpenAI's table maps a dated snapshot id to its undated name, so a snapshot of a listed or retired model is priced through that mapping.
- **R4.** Refusing a retired model without `+passthrough` names its retirement date:
  `gpt-5.5 was retired on 2026-11-01; listed openai models: gpt-6.1-sol, gpt-6-luna, gpt-6-astra, gpt-5.6-terra, gpt-5.6-luna; add +passthrough to run it anyway`.
- **R5.** Every provider field is typed `bool`, `int`, `str` or a `Literal` of strings, or one of those `| None` with the default `None`.
- **R6.** The base class refuses a model containing `+`, `,` or whitespace, and a `str` field value containing `,` or whitespace, since recipe text cannot write them.

Loading a declaration refuses one that breaks R2, R3 or R5, as it refuses a malformed table.
The models the price tables hold today without listing them were admitted verbatim until #1143 landed on 2026-11-01, so each gets that date.
A model delisted later records its own.

The recipe classes after the change:

| provider | wire | fields, with their defaults | listed models |
|---|---|---|---|
| `openai` | `responses` | `reasoning_effort`: `none`, `minimal`, `low`, `medium`, `high`, `xhigh` or `max`, default unset; `service_tier`: `auto`, `default`, `flex` or `priority`, default unset; `compact_threshold`: positive `int`, default unset | `gpt-6.1-sol`, `gpt-6-luna`, `gpt-6-astra`, `gpt-5.6-terra`, `gpt-5.6-luna` |
| `deepseek` | `chat` | `reasoning_effort`: `none`, `low`, `high` or `max`, default unset | `deepseek-v4-pro`, `deepseek-flash` |
| `anthropic` | `messages` | `effort`: `low`, `medium`, `high`, `xhigh` or `max`, default unset; `fast_mode`: `bool`, default `false` | `claude-opus-5`, `claude-opus-5-5`, `claude-sonnet-5`, `claude-sonnet-5-5`, `claude-fable-5`, `claude-fable-5-1`, `claude-haiku-4-5-20251001`, `claude-haiku-5-5` |
| `echo` | `echo` | none | `echo`, priced at zero |

DeepSeek's `reasoning_effort` and Anthropic's `effort`, typed `str` today, become `Literal`s (R5), and Anthropic's no longer validates against core's levels.
DeepSeek's mapping of neutral effort levels onto its own scale goes with the neutral levels:
`reasoning_effort` takes DeepSeek's own values, the only ones its recipes have ever recorded.
DeepSeek's check that a model id has the shape `deepseek-*` goes with `MODEL_ID`, so `+passthrough` admits any id, as for every provider.
Anthropic's `effort` loses its default `xhigh`, so a recipe without `effort` leaves effort to Claude Code (§7).
Echo's `PRICE_TABLE` becomes a price table with one zero row, so R3 holds for it as for every provider.
OpenAI's fast mode is `service_tier=priority`, and Anthropic's is `fast_mode=true`.

### 3.6 Harnesses

A harness serves the providers its `llm_providers` names, read from the declarations' classes:
`bro` the providers whose `WIRE` has an installed wire client (`openai`, `deepseek`, `echo`), `claude` only `anthropic`.
`Harness.check_recipe(recipe)` replaces `Harness.resolve_recipe(base, selection)`:
it refuses a recipe whose provider the harness does not serve, naming the installed harnesses that serve it, as today's refusal does.
A recipe never switches the harness.

There is no deferred model:
the Anthropic class's `model` is a required `str`, `_SESSION_DEFAULT` goes, and a Claude session always receives `--model` (§7).

## 4. Homes

### 4.1 A bro's home

- **H1.** A distribution registers bros in entry-point groups named `bro.bros.<home>`, one entry per bro, each pointing at the bro's class.
  The `bro` group goes.
- **H2.** A bro's home is the `<home>` of the group that registers it.
  A distribution may register bros of several homes.
- **H3.** The registry reads the groups from installed metadata without importing any bro module, as it reads the `bro` group today,
  and still refuses two entries claiming one bro name, in any homes, naming the distributions that declare them.

```toml
# dev/pyproject.toml
[project.entry-points."bro.bros.bro"]
dev = "bros.dev:Dev"
eyebro = "bros.eyebro:Eyebro"
lead = "bros.lead:Lead"
terminal = "bros.terminal:Terminal"
```

In this repository `bro`, `bro-browser`, `bro-dev`, `bro-local` and `bro-oops` register their bros in `bro.bros.bro`.
ppp registers its bros in `bro.bros.ppp`, so `ppp-dev`, subclassing the `bro-dev` distribution's `dev`, has the home `ppp`.

### 4.2 Home settings

- **G1.** A home ships settings through one entry of the group `bro.settings`:
  the entry's name is the home, and its value names the package that holds the settings as the resource `bro-settings.toml`.
- **G2.** `bro-settings.toml` is one layer in the schema of §5.1, written in TOML.
  It is package data, so it is installed with its distribution and carried by every runtime bundle frozen from the installation.
- **G3.** A home with no `bro.settings` entry ships no settings.
  Two installed distributions with a `bro.settings` entry for one home refuse every launch, naming both.
- **G4.** A home may ship settings without registering bros, and register bros without shipping settings.

```toml
# local/pyproject.toml
[project.entry-points."bro.bros.bro"]
bro-dev = "bros.bro_dev:BroDev"
bro-eyebro = "bros.bro_eyebro:BroEyebro"
bro-watch-probe = "bros.bro_watch_probe:BroWatchProbe"
bro-file-probe = "bros.bro_file_probe:BroFileProbe"

[project.entry-points."bro.settings"]
bro = "bro.local"
```

```toml
# local/bro/local/bro-settings.toml
default-bro = "bro-dev"
harness = "claude"
summon-harness = "bro"
```

`bro-local` is the only distribution of this repository that ships the `bro` home's settings, and no other repository installs it,
so those settings apply only in this repository's own installation.
They take the keys the root `pyproject.toml`'s `[tool.bro]` holds today, with `default` renamed `default-bro` (K1).

### 4.3 The installation's home

- **I1.** An installation names its home in the marker file `bro-installation-home` at the root of its environment (`sys.prefix`), holding the home's name and a final newline.
- **I2.** Whatever builds an environment that launches run from writes the marker directly:
  this repository's `./setup.sh` gains `printf 'bro\n' > .venv/bro-installation-home` after its sync, outside the condition that skips the sync;
  the CI workflow, which syncs without `setup.sh`, writes the same line;
  and a consumer's setup writes its own home.
  Tests set the installation's home through a fixture, as they isolate the host configuration.
- **I3.** A frozen runtime bundle is rebuilt from the installation's distributions, not copied from its environment, so its manifest records the installation's home:
  bundles of two homes never share a hash, a bundle re-frozen from inside itself lands on itself, and materializing a bundle writes the marker into its host and container environments.
  Every session and summon of a root therefore sees the root's home;
  in-process `bro run` and `bro chat` read the marker from the live environment.
  The benchmark's bundle is an installation of its own, whose home is `benchmark` (§12.2).
- **I4.** A missing marker, or one whose content is not a well-formed home name, refuses every launch from the installation, naming the marker's path and the line to write.
- **I5.** The marker may name any well-formed home, one the installation's distributions declare or not:
  a home with neither bros nor settings is configured through `homes.<home>` alone.
- **I6.** The installation's home settings are the settings its home ships (§4.2), if any;
  every other home's settings are never read.

## 5. Configuration

### 5.1 Layers and their keys

One schema serves every layer;
the table says which keys each level takes.

| key | value | home settings | `defaults` | `homes.<home>` | `homes.<home>.bros.<bro>` |
|---|---|---|---|---|---|
| `default-bro` | the default bro | yes | — | yes | — |
| `harness` | a harness name, for a root launch | yes | yes | yes | yes |
| `summon-harness` | a harness name, for a summon | yes | yes | yes | yes |
| `summon-depth` | a non-negative `int` (§5.6) | yes | yes | yes | yes |
| `llm` | an object from harness name to recipe text | yes | yes | yes | yes |
| `grant`, `revoke`, `limit` | lists in the authority syntax | yes | yes | yes | yes |
| `creds` | `kind+instance` picks | — | — | yes | yes |
| `bros` | an object from bro name to bro layer | — | — | yes | — |

- **K1.** `default-bro` names the bro that `dive-in` and `ride scope` take when none is named:
  the installation's home settings and `homes.I` set it, the latter winning (L1).
  `defaults` cannot set it, since each home has bros of its own.
- **K2.** `creds` selects stored credential instances, which only the host may name, as today.
- **K3.** `llm` maps each harness to the recipe text the bro runs on it:
  `{"claude": "anthropic:claude-opus-5-5,effort=xhigh", "bro": "openai:gpt-5.6-terra,reasoning_effort=medium"}`.
- **K4.** `~/.bro.json`'s top level takes `defaults`, `homes`, `user` and `operator`.
  `user` and `operator` keep their meaning.
  The top-level `harness`, `summon-harness`, `summon-depth` and `llm` go:
  the first three move into `defaults`, and `llm` (presets) leaves.
- **K5.** A key of `homes` is a home's name.
  `projects`, its path and URL keys, and every reading of configuration from the attached repository go, except the tree's own keys (§5.7).

### 5.2 The layer stack

For a bro `B` whose home is `H`, launched from an installation whose home is `I`, the layer stack is, least specific first:

1. `I`'s home settings;
2. `defaults`;
3. `homes.I`;
4. `homes.H.bros.B`, when `H` differs from `I`;
5. `homes.I.bros.B`.

The launch's own choices then apply over the stack.

- **L1.** For `default-bro`, `harness`, `summon-harness` and `summon-depth`, the last layer setting the key wins;
  §5.6 says how the `summon-depth` winners make an allowance.
- **L2.** For `llm`, the last layer whose map has the harness as a key wins, harness by harness:
  a bro layer that names only `bro` leaves the `claude` recipe to the layers before it.
- **L3.** `creds` keeps today's rule:
  per kind, the last layer picking it wins over the store's `creds.json` defaults.
- **L4.** `grant`, `revoke` and `limit` fold in stack order, each layer applying its caps and then its grants, as the configuration fold does today (`bro/reference/permissions.md`, "Readings, bases and configuration").
- **L5.** A summon's stack is the child bro's, in the root's reading:
  the child's own home and bro entries, not the summoner's.
- **L6.** A layer the configuration does not have contributes nothing:
  a host with no `~/.bro.json` runs on the home settings alone, and a home without settings starts its stack at `defaults`.

Home settings are the least specific layer, so every layer the host writes overrides what a home ships.

### 5.3 Harness selection

A root launch runs on `--harness` when given, otherwise on the stack's `harness`.
A summon runs on its `harness` field when given, otherwise on the child's stack's `summon-harness`.
A manual summon's commit, which the operator launches the way a root is launched, runs on the operator's `--harness` when given, otherwise on the child's stack's `harness`.
With neither, the start is refused:
`no harness for <bro>: pass --harness, or set "harness" in a layer of ~/.bro.json or in the home settings`, naming `summon-harness` for a summon.
The selected name must be an installed harness.
`ride resume` keeps the recorded harness.

### 5.4 What a launch reads and validates

At a root's start, the reading reads the installation's marker, its home settings and `~/.bro.json`.

- **V1.** The installation owns its home settings, `~/.bro.json`'s top level, `defaults`, and `homes.I`, every bro layer in it included.
  It validates them strictly and in full:
  an unknown key is refused, and so is a value of the wrong shape;
  a bro under `homes.I.bros` or named by `default-bro`, and every type, option, bro target and credential kind an authority entry or pick names, must be registered in the installation;
  and a recipe under a harness the installation has is bound in full (§3.2), its provider installed, its fields valid and its model admitted.
- **V2.** A recipe under a harness the installation lacks is checked by `parse_recipe` alone, since another installation reading the same file may run it.
- **V3.** Of the rest of `homes`, the reading reads only the foreign entries `homes.H.bros.B` for the installed bros `B` whose home `H` differs from `I`.
  At the start it checks only their shape:
  known keys, value shapes, recipe text by `parse_recipe`, well-formed names.
- **V4.** A foreign entry's value is bound in full only when a start uses it:
  a recipe when it wins under L2, a harness when it wins under L1, a credential pick and the authority entries whenever its bro starts.
  What the installation cannot honor refuses that start alone, naming the layer and both ways on:
  install a plug-in that provides what it names, or override the value in `homes.I.bros.B`.
- **V5.** A value of `homes.I.bros.B` shadows the foreign entry's value for the same key under that key's rule (L1 to L3), and a shadowed value is never bound.
  A foreign grant does not shadow, since grants fold (L4):
  the installation answers a foreign grant it does not want with a `revoke` in `homes.I.bros.B`.
  A foreign grant naming something the installation does not register cannot be revoked, since its own layers name only what it registers (V1);
  it refuses its bro's starts until the plug-in is installed.
- **V6.** Nothing else under `homes` is read, so another home's mistakes reach this installation only through the foreign entries of bros it installs, and only at the starts that use them.
- **V7.** A failure under V1 refuses every launch from the installation, naming the layer and the key:
  `~/.bro.json: homes.bro.bros.bro-eyebro.llm.bro: unknown field 'effort' for openai; fields: reasoning_effort, service_tier, compact_threshold`.

### 5.5 The file

```json
{
  "defaults": {
    "harness": "claude",
    "summon-harness": "bro",
    "summon-depth": 2,
    "llm": {
      "claude": "anthropic:claude-opus-5-5,effort=xhigh",
      "bro": "openai:gpt-5.6-terra,reasoning_effort=medium"
    },
    "grant": ["bro.placement=join"]
  },
  "homes": {
    "bro": {
      "creds": ["brog+github", "github+dev"],
      "bros": {
        "bro-dev": {"llm": {"bro": "openai:gpt-6.1-sol,reasoning_effort=high"}},
        "bro-eyebro": {
          "summon-depth": 0,
          "llm": {"bro": "openai:gpt-6.1-sol,reasoning_effort=xhigh", "claude": "anthropic:claude-opus-5-5,effort=max"}
        },
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

### 5.6 The summon-depth allowance

- **D1.** A bro's allowance is how many more generations of missions may start below it;
  a bro may launch a mission of any type, a summon, a join, a webview or a benchmark, only while its allowance is at least 1.
- **D2.** A bro's **cap** is the `summon-depth` of its bro layers, `homes.H.bros.B` and `homes.I.bros.B`, the last setting it winning, as for every key (L1):
  the installation's own entry overrides a foreign one.
- **D3.** A root's allowance is the `summon-depth` of its stack's home settings, `defaults` and `homes.I`, the last setting it winning, lowered to its cap when it has one.
- **D4.** A launched mission's allowance is its launcher's allowance minus 1, lowered to its cap when it is a bro that has one.
  A cap only lowers the allowance a bro receives, never raises it.
- **D5.** With no `summon-depth` anywhere, a root's allowance is 2, today's default.
- **D6.** Each mission records its allowance.
  Launch control checks the launcher's allowance wherever it checks today's summon depth:
  at every launch, and at a manual summon's commit;
  a manual summon's prepare is not checked, as today.
- **D7.** A resumed session is a root, so its allowance is a root's (D3), whatever it was when the session was summoned.

With the file of §5.5, a root of any bro has the allowance 2, its children 1, and their children 0, as today;
`bro-eyebro` has the allowance 0 wherever it starts, so it launches nothing.

### 5.7 The attached tree

A ride follows its installation's configuration (§5.2);
of the attached repository it reads only what describes the tree.

- **P1.** Of the attached tree's `[tool.bro]`, a ride reads `build-context-command` and sub-tables such as `[tool.bro.devoops]`, whose keys belong to whoever declares them.
  It reads no other key, so a tree from another home, or from a commit before this design, attaches with whatever else its `[tool.bro]` holds.
- **P2.** A boxed session on an attached tree with a `uv.lock` runs in the tree's project image, tagged `bro/ride-project:<hash>`, where the hash covers the runtime image and the tree's manifests as today.
  The image carries the label `ride-attachment=<the attachment's identity>`, its path or URL.
- **P3.** After building a project image, ride untags the other unlocked project images labeled with the same attachment, and the cleanup that removes root-owned files looks up a present image of the tree by that label;
  so each attachment keeps one project image, as each image repository name does today.

## 6. Resolution

### 6.1 The resolution function

`resolve_recipe(reading, bro, harness, explicit=None)` (`ride/ride/authority_scope.py`) returns the resolved recipe and its source.

1. When `explicit` recipe text is given, it is the text, and the source is the surface that passed it (§6.2).
   Otherwise the text is the winning `llm` entry for the harness under L2, and the source is that layer's name.
   With neither, the start is refused:
   `<bro> has no recipe for harness <harness>: pass --llm, or set llm.<harness> in a layer of ~/.bro.json or in the home settings`.
2. The text is bound (§3.2).
3. The harness must serve the recipe (§3.6).

Nothing is merged across sources:
an explicit text replaces the configured one whole, and two layers never combine into one recipe.

### 6.2 Surfaces

| surface | explicit text | harness | reading | source when explicit |
|---|---|---|---|---|
| `ride solo`, `ride along` | `--llm` | `--harness`, else the stack's `harness` | the root's | `--llm` |
| `dive-in` | `--llm`, forwarded | as `ride along` | as `ride along` | `--llm` |
| a summon (tool, `summon` CLI) | `llm` | `harness`, else the child's stack's `summon-harness` | the root's (§6.4) | `summon llm` |
| a manual summon's commit | the operator's `--llm` | the operator's `--harness`, else the child's stack's `harness` | the root's | `--llm` |
| `ride resume` | `--llm`, else the recorded recipe (§8) | the recorded harness | a fresh reading | `--llm` |
| `ride scope` | `--llm` | `--harness`, else the stack's `harness` | a fresh reading | `--llm` |
| `bro run`, `bro chat` | `--llm` | `bro` | the live installation's | `--llm` |
| `bro chat --fork <trail>` | `--llm`, else the trail's recorded recipe (S3) | `bro` | as `bro chat` | `--llm` |

- **S1.** `ride scope` prints the resolved recipe's canonical text and source, or the refusal a launch would meet, exiting nonzero on a refusal;
  without `--bro` it takes the installation's `default-bro` (K1).
  It no longer needs `--repo` to see configuration, since configuration follows the installation.
- **S2.** `bro run` and `bro chat` resolve through the same function and stack as a managed launch, on the bro harness.
  They read only the stack's `llm`; their credentials stay ambient, as today.
  The managed native session passes `--resolved-recipe`, the resolved-recipe record its launcher wrote (§7), which `bro run` and `bro chat` take verbatim and `wires.open` still checks (R1).
- **S3.** `bro chat --fork <trail>` without `--llm` runs the trail's recorded recipe, as `ride resume` runs the recorded one:
  `bro.fork.fork` rebuilds the header's recipe with `Recipe.from_dict` and checks it (R1), and its source is `trail <id>`, naming the forked trail.
  With `--llm`, the explicit recipe runs;
  one on another wire than the trail's is refused, as today.
- **S4.** `bro.native.llm`, the provider diagnostic, takes a required `--llm` and reads no configuration.
- **S5.** `mu` binds its material's recipe texts (`llm` and each role's) with `bind_recipe`, with no layers and no harness;
  the recipe's wire must have an installed client.
- **S6.** A benchmark agent preset's Harbor `model_name` is `<provider>/<rest of the recipe text>`, such as `openai/gpt-5.6-terra,reasoning_effort=high,compact_threshold=200000`;
  Harbor splits it once at `/` and keeps the rest as written (§17).
  `harbor_agent.llm_recipe` joins the two parts with `:` and checks the result with `parse_recipe`;
  a preset without `model_name` is refused when the agent is constructed.
  The trial writes into its task container a host configuration whose `defaults` set `harness` and `summon-harness` to the preset's harness and `llm` under that harness to the recipe,
  so the trial's root and every summon in it resolve through the ordinary stack, from `defaults`;
  the trial's `ride solo` passes neither `--harness` nor `--llm`.

### 6.3 Authority

The bro worker type's LLM options are `provider` (a provider name), `model` (a string) and `passthrough` (a flag), all open, none overridable.
`effort`, `fast` and the `~default` symbol go, and so does `bro/authority`'s `Symbol` value, which only `~default` used.

- **A1.** A start's request carries the explicit recipe text, if any, as ungoverned content under `llm`.
- **A2.** The descriptor's `provider`, `model` and `passthrough` are the resolved recipe's, whether it came from the request or from configuration,
  so a cap on any of them refuses the start, a configured default included, and never changes it.
- **A3.** Admission recomputes the resolved recipe and refuses a start whose descriptor names a different provider, model or `passthrough`.

### 6.4 When configuration is read

A root reads the installation's marker, its home settings and `~/.bro.json` once, at its start.
Its summons resolve against that reading, so an edit made later reaches only roots started after it;
`ride override` remains the way to change a running root's authority.

## 7. Into the session

The resolved-recipe record `{"recipe": <dump>, "source": <source>}` has one shape wherever it travels:
the start's `resolved_recipe`, a manual summon's commit, the resume record (E1), and the session's `RIDE_RESOLVED_RECIPE`.

- The native harness passes the record to `bro run` or `bro chat` as `--resolved-recipe`, which runs its recipe verbatim.
- The Claude harness passes `--model <model>` always, `--effort <effort>` when the recipe sets `effort`, and `"fastMode": <fast_mode>` in its `--settings`.
  A recipe without `effort` leaves effort to Claude Code, as an unset field leaves it to the vendor (B4).
- The Claude recorder records the recipe in the trail header, as today;
  each call records the model that answered.

## 8. Resume

- **E1.** A session's resume record keeps its resolved-recipe record (§7).
- **E2.** `ride resume` runs the recorded recipe unless `--llm` replaces it;
  a replacement is resolved as on `ride along`, on the recorded harness, and its source is `--llm`.
  A native session continues its trail on one wire, so a replacement on another wire than the recorded recipe's is refused before the session starts.
- **E3.** Before the session starts, `ride resume` checks the recorded recipe, rebuilt with `Recipe.from_dict`, for admission (R1) and against the recorded harness (§3.6).
  One whose model has since been retired is refused with R4's message;
  `ride resume --llm` with a listed model, or the same model with `+passthrough`, resumes it.
- **E4.** A record written before this design has no resolved recipe, and its request may carry the options this design removes (`effort`, `fast`, `~default`).
  `ride resume` recognizes such a record and refuses it, naming the workspace and the two ways on:
  `bro chat <bro> --fork <trail>` for a native session, or a new session;
  `ride list` still lists it.
  A record naming a materialized runtime bundle (`--runtime-bundle`) re-executes that bundle's `ride` before reading the record, so it resumes under the code that wrote it.

## 9. What the session and the tools show

- The banner (`bro::banner`, `ride banner`) gains the line `llm: <canonical text> (<source>)`.
  The `banner` tool renders its run's own resolved recipe, passed in-process like the bro's name and trail id, so a `bro run` inside a session shows its own recipe rather than the session's.
- `ride banner`'s switch for plain `key: value` lines is `--plain`, since `--llm` names recipe text on every other command.
- `ride scope` prints the same line, or the refusal (S1).
- `bro show` shows each bro's home and no recipe or LLM-key line, since a bro declares no recipe.

## 10. Failure behavior

| condition | raised by | message names | before |
|---|---|---|---|
| recipe text off the grammar, a repeated flag or field, an unknown flag | `parse_recipe` | the text and the part | any provider is loaded |
| an uninstalled provider | `bind_recipe` | the provider, the installed ones | the start |
| an unknown field, a value of the wrong type, an invalid combination | `bind_recipe` | the field, the class's fields or the value's type | the start |
| an unlisted model without `+passthrough` | `bind_recipe`, `ride resume`, `bro.fork.fork`, `wires.open` (R1) | the model, the listed ones, `+passthrough`, and the retirement date of a retired model | the start, or the request |
| a recipe the harness does not serve | `Harness.check_recipe` | the harness, the harnesses that serve it | the start |
| no recipe for the harness | `resolve_recipe` | the bro, the harness, where to set one | the start |
| no harness | §5.3 | the bro, where to set one | the start |
| a missing or malformed marker | the reading (I4) | the marker's path, the line to write | every launch |
| two `bro.settings` entries for one home | the reading (G3) | both distributions | every launch |
| a malformed part the installation owns | the reading (V7) | the layer and key | every launch |
| a foreign entry's value the installation cannot honor | the start using it (V4) | the layer, what to install, the override to add | that start |
| two entries claiming one bro name | the registry (H3) | both distributions | the registry is read |
| a declaration breaking R2, R3 or R5 | `providers.declaration` | the model or field | the provider is used |
| a launch beyond its launcher's allowance | launch control (D6) | the allowance and the layers that set it | the launch |
| a descriptor that differs from the resolved recipe | admission (A3) | the option | the start |
| a native resume's replacement on another wire | `ride resume` (E2) | both wires | the session starts |
| a resume record from before this design | `ride resume` (E4) | the workspace and the ways on | the resume |
| a benchmark preset without `model_name` | the agent (S6) | the preset | the job runs a trial |

## 11. What goes

| mechanism | replaced by |
|---|---|
| `LLMSelection`, `over`, `parse`, `parse_with_presets`, `is_preset`, `resolve`, `resolve_model`, `default_spec`, `available_specs`, `models`, `effort_values`, `carriers`, `provider_of_model` | `parse_recipe`, `bind_recipe`, `format_recipe` (§3), and the declarations' classes (§3.5) |
| `LLMSelectionError` | `RecipeError`, raised by every refusal of §3 |
| the names `LLMSpec`, `llm_spec`, `resolved_llm`, `RIDE_RESOLVED_LLM` and `--resolved-llm` | `Recipe`, `recipe`, `resolved_recipe`, `RIDE_RESOLVED_RECIPE` and `--resolved-recipe`, carrying the record of §7; the user-facing `--llm` and the `llm` key keep their names |
| `--provider`, `--model`, `--effort`, `--fast`, `--passthrough` | `--llm` with complete recipe text |
| `[tool.bro.llm]` and the host's top-level `llm` presets | nothing, until presets return |
| `MODELS`, `DEFAULT_MODEL`, `MODEL_ID`, Anthropic's `DEFAULT_EFFORT` | full model ids in recipe text, admitted by `MODEL_INPUTS`; retired ones in `RETIRED_MODELS` |
| `BaseBro.llm_spec` and the five bros' declarations | `llm` in configuration; the run's recipe passed to the runner explicitly |
| `_SESSION_DEFAULT`, the Anthropic class's `model=None`, `deferred_fields`, `default` in host lists, `~default` | a model in every recipe |
| `EFFORT_LEVELS`, `with_effort`, `fast`, `effort_values`, `selection_fields`, `selection`, `with_selection` | the provider's own fields |
| the host's ordered `llm` lists and first-served selection | `llm` maps keyed by harness (K3, L2) |
| the authority options `effort` and `fast` | none, until authority over the whole recipe |
| the `bro` entry-point group | `bro.bros.<home>` groups (H1) |
| `projects` keyed by path and URL, and `[tool.bro]` read from the attached repository for anything but the tree | `homes` keyed by home name, the installation's marker and home settings (§4, K5, P1) |
| `[tool.bro]`'s `default` | `default-bro` in home settings or `homes.<home>` (K1) |
| `image-repository`, and project images named after the default bro | `bro/ride-project` images labeled with their attachment (P2, P3) |
| the host's top-level `harness`, `summon-harness`, `summon-depth` | the same keys in layers (§5.1) |
| the root-wide summon depth | the allowance (§5.6) |
| `Harness.resolve_recipe`, `Harness.runs`, `SessionHarness.resolve_llm`, the reading's per-bro base recipes | `Harness.check_recipe`, `resolve_recipe` (§6.1) |
| `ride banner --llm`, the plain-lines switch | `ride banner --plain` (§9) |
| `canonicalize`, `drop_piece_flags`, `host_config.launch_llm`, `launch_selection`, `summon_depth`, `LLMDefault` | nothing: they have no caller outside tests |

## 12. Records, other processes and rollout

### 12.1 Records

- Trail headers keep recording `dump()`.
  Recorded headers stay readable:
  `rewind`, cost, the trail projections and the benchmark read their fields as stored, and only `bro.fork.fork` rebuilds a header's recipe, from native trails, whose recipes always name a model (S3).
  An Anthropic header recorded with `model: null` comes from the Claude harness only and is never rebuilt.
- Every model a recorded trail ran is listed or retired, or a dated snapshot of one, so its cost is still priced (R3).
- Resume records gain the resolved-recipe record (E1);
  older ones are refused (E4).
- Benchmark retention keeps the trial's `model_name` as written.

### 12.2 Contracts with other processes

- **`~/.bro.json`** is read by every installation on this host and by those on a second host that shares the file through dotfiles.
  An installation from before this design refuses the new schema, and this design's refuses the old one.
  An old installation parses the whole file wherever it reads a credential outside a session, not only at a launch,
  so from the rewrite until it moves past this design it can neither launch nor read a credential:
  the `credentials` CLI, in-process `bro run` and `bro chat`, and the benchmark's `retain` and `publish` all refuse.
- **A consumer's move** past this design takes, in its repository:
  its framework pin past this design;
  its bro groups renamed `bro.bros.<home>`;
  its marker written by its setup;
  its `[tool.bro]` launch and policy keys moved into its home settings or `homes.<home>` (P1);
  `llm_spec`, `[tool.bro.llm]` and `image-repository` dropped;
  its `--llm` texts rewritten as recipe text;
  and a direct dependency on every provider plug-in its recipes name.
  `bump-bro` carries such a move.
  A host that only installs the framework moves by re-syncing its installations.
- **Hosts sharing the file** stay in step after the rollout as well:
  V1 refuses every launch on a home section naming a bro or provider the installation lacks,
  so the second host re-syncs whenever the file starts naming one.
- **The entry-point groups** are read only by the installation that holds them, so the rename has no order beyond each repository's own.
- **`mu` material** is copied into a session's store at its start and parsed at each call.
  A session started before the rewrite keeps its copy and keeps working;
  what fails is anything started after it on a runtime from before this design:
  a root, a summon by a root started earlier, and an old benchmark bundle.
- **The benchmark bundle** is built from the checkout, not from the host installation;
  building it writes its marker and records its home `benchmark` in its manifest, whose format the landing changes.
  `benchmark-run` and `benchmark-job` rebuild a bundle of another format, so no bundle from before this design runs a trial after it, unless kept with `--keep-bundle`.
  A trial reads no host file but the one it writes (S6) and a store hydrated from the host's, `mu+benchmark` included,
  so no benchmark runs between the landing and the rewrite, when new code would meet the old material.
- **The summon payload** is read only by processes of the root's runtime bundle, so its `llm`-only shape needs no order.
- **The hosted trails server** decodes responses and checks only that a header's `native.llm` is an object;
  it parses no recipe text and loads no provider, so it is not redeployed.

### 12.3 Rollout order

1. The landing merges.
2. The interactive configuration step (§12.4) writes `~/.bro.json.next` and the new `mu` material beside the current files, and validates them with the landed code.
3. Every other root on the host ends.
4. The host's installation re-syncs to the landed commit, which writes its marker;
   the two new files replace the current ones, with backups kept.
5. Fresh roots start.
6. ppp's and kap's installations move (§12.2), each through a session on its own repository.
7. The owner moves the second host.

Rollback before step 6 restores the two backups and re-syncs the previous commit.

### 12.4 The interactive configuration step

The last step of the implementation plan is attended and needs the host's own files, so the owner launches it as a manual, unboxed session before the re-sync, on the runtime the host still runs.

1. It reads what each installed bro does, in this installation and in ppp's and kap's:
   its persona prompt, tools and spells (`bro show`), and the bros it summons.
2. It reads the current `~/.bro.json` and `mu` material.
3. It proposes the new file:
   `defaults` with a recipe per harness;
   for each home, its credential picks and grants, carried over from the URL entries they sit under today;
   and, for each bro whose work calls for a different model, knob or allowance, a bro layer with the recipe and the reason.
   It proposes the `mu` material's recipes in the new syntax.
4. The owner settles every entry.
5. It writes `~/.bro.json.next` and the new `mu` material beside the current files, and validates them with the landed code,
   in a worktree of the landed commit that `./setup.sh` has synced and given its marker.
   There it runs `ride scope --bro <bro> --harness <harness>` for every bro of this installation on every harness,
   with `HOME` set to a scratch directory holding the candidate as `.bro.json`, `BRO_STORE` set to the host's store and `UV_CACHE_DIR` to the host's uv cache;
   and it runs `bro.native.llm --llm <text>` once for each recipe of the `mu` material.
   ppp's and kap's sections are validated by their own moves (step 6 of §12.3), since this installation reads no section of theirs (V6).

## 13. Examples

The examples read the file of §5.5 from this repository's installation (`I` = `bro`), unless they say otherwise.

1. `ride along bro-dev` with no `--harness`:
   the stack's `harness` is `claude`, from `defaults`, which overrides the `bro` home settings;
   the only `claude` entry is in `defaults`, so the recipe is `anthropic:claude-opus-5-5,effort=xhigh` from `defaults`;
   Claude Code starts with `--model claude-opus-5-5 --effort xhigh` and `fastMode` false.
2. A summon of `bro-dev` with no `harness`:
   the child's `summon-harness` is `bro`, from `defaults`;
   `homes.bro.bros.bro-dev.llm.bro` wins over `defaults.llm.bro` (L2), giving `openai:gpt-6.1-sol,reasoning_effort=high` from `homes.bro.bros.bro-dev`.
3. A summon of `bro-dev` with `llm: "anthropic:claude-opus-5-5"` and no `harness`:
   the harness is `bro`, which does not serve `anthropic`, so the summon is refused, naming the `claude` harness.
4. `ride along --harness bro --llm openai:sol eyebro`:
   binding refuses `sol`, which is not a listed `openai` model, naming the five listed ones and `+passthrough`.
5. `ride along --harness bro --llm openai:gpt-5.5 eyebro`:
   refused with `gpt-5.5 was retired on 2026-11-01`, the listed models and `+passthrough` (R4).
6. `ride along --harness bro --llm openai:gpt-7+passthrough,reasoning_effort=high eyebro`:
   admitted by `passthrough`;
   the banner shows `llm: openai:gpt-7+passthrough,reasoning_effort=high (--llm)`.
7. `terminal` summoned on `bro`:
   `openai:gpt-5.6-terra,compact_threshold=200000` from `homes.bro.bros.terminal`, whose unset `reasoning_effort` is left to OpenAI.
8. From ppp's installation (`I` = `ppp`), a summon of `browser`, whose home is `bro`:
   the stack is the `ppp` home settings, `defaults`, `homes.ppp`, `homes.bro.bros.browser`, then `homes.ppp.bros.browser`, which the file lacks;
   `browser`'s `creds` come from `homes.bro.bros.browser`, and its `bro` recipe from `defaults`.
9. The same summon, with `homes.bro.bros.browser.llm.bro` set to `deepseek:deepseek-flash` and no DeepSeek plug-in in ppp's installation:
   ppp's launches proceed, since the foreign entry is only shape-checked at the start (V3);
   the summon of `browser` on `bro` is refused, naming `homes.bro.bros.browser.llm.bro`, the missing provider `deepseek`, and `homes.ppp.bros.browser` as the place to override (V4).
   Setting `homes.ppp.bros.browser.llm.bro` shadows the foreign recipe (V5).
10. A typo under `homes.ppp`:
    this repository's installation never reads `homes.ppp` (V6), so its launches proceed;
    every launch from ppp's installation is refused, naming the layer and key (V7).
11. `homes.bro.bros.bro-eyebro.llm.bro` = `openai:gpt-6.1-sol,effort=xhigh`:
    every launch from this repository's installation is refused (V7), since `effort` is not an OpenAI field.
12. A root `lead` summons `bro-dev`, which summons `bro-eyebro`:
    `lead`'s allowance is 2, `bro-dev`'s 1, and `bro-eyebro`'s 0, both as its summoner's allowance minus 1 and as its cap (D4);
    a summon or a webview launched by `bro-eyebro` is refused (D6).
13. `ride resume <workspace>` of a session that ran `openai:gpt-6.1-sol,reasoning_effort=high`:
    it runs the same recipe, with its recorded source, whatever the configuration now says;
    `ride resume --llm openai:gpt-6.1-sol,reasoning_effort=max <workspace>` runs that instead, with the source `--llm`,
    and `ride resume --llm deepseek:deepseek-v4-pro <workspace>` is refused before the session starts, since `chat` is not the trail's `responses` wire (E2).
14. `bro run dev 'hi'`:
    `bro run` resolves on the `bro` harness through the same stack, so `dev` runs `openai:gpt-5.6-terra,reasoning_effort=medium` from `defaults`.
15. `bro chat dev --fork <id>` of a trail recorded with `openai:gpt-5.6-terra,reasoning_effort=medium`:
    it runs that recipe, with the source `trail <id>` (S3).
16. An installation whose marker is missing:
    every launch is refused, naming `<environment>/bro-installation-home` and the line to write (I4).
17. `ride along --into <a commit before this design> bro-dev`:
    that commit's root `[tool.bro]` still holds `default`, `harness` and `summon-harness`, which the launch does not read (P1),
    so it runs on the installation's configuration like any other launch.
18. The benchmark preset `terminal-bro-gpt-5.6-terra-high`, whose `model_name` is `openai/gpt-5.6-terra,reasoning_effort=high,compact_threshold=200000`:
    the trial's host configuration sets `defaults.harness` and `defaults.summon-harness` to `bro` and `defaults.llm.bro` to `openai:gpt-5.6-terra,reasoning_effort=high,compact_threshold=200000`,
    so the root `terminal` and a `terminal` it summons both run it, from `defaults` (S6).
19. Recipe texts:
    `openai:gpt-5.6-terra` binds with every field at its default, and its canonical text is the same;
    `openai:gpt-5.6-terra,service_tier=priority,reasoning_effort=high` binds, and its canonical text is `openai:gpt-5.6-terra,reasoning_effort=high,service_tier=priority`;
    `anthropic:claude-opus-5-5,fast_mode=yes` is refused, since `fast_mode` is a `bool`;
    `openai:gpt-5.6-terra,reasoning_effort=high,reasoning_effort=low` is refused (T2);
    `openai:gpt-5.6-terra+fast` is refused (T3).

## 14. Decision record

The `## Step 2 outcome` of #1150, now in its description's edit history, records the decisions D1 to D7, S1 to S3, C1 to C5 and L1 to L5, all settled by the owner on 2026-10-31;
this design states them as the rules above, with the amendments below.
N1 to N24 were raised while designing, and N25 to N33 in the design review;
the owner settled N1 to N8 and N22 to N24 in the design discussion of 2026-11-01, and every other one in the review of 2026-11-01, amending some as the table says.

| id | decision | status |
|---|---|---|
| N1 | retired models: `RETIRED_MODELS` with the framework's retirement dates, the price table exactly the listed and retired models, today's unlisted price rows retired on 2026-11-01 (R2 to R4) — amends C5 | settled |
| N2 | "home" names the concept everywhere, "project" and "site" are dropped, and the host configuration's key is `homes` | settled |
| N3 | bros register in `bro.bros.<home>` groups, replacing the `bro` group (H1 to H3) | settled |
| N4 | a home ships settings through a `bro.settings` entry naming the package that holds `bro-settings.toml` (G1 to G4) | settled |
| N5 | the installation names its home in the marker `<environment>/bro-installation-home`, written by setup, with no command and no fallback (I1 to I6); frozen bundles carry it in their manifest (I3) | settled |
| N6 | foreign entries are shape-checked at the start, bound at use, shadowed by the installation's own bro entry, and answered with `revoke` for grants (V3 to V6) | settled |
| N7 | `summon-depth` is an allowance that a bro's cap can only lower, the cap taken from its bro layers by the usual last-wins rule (§5.6) | settled |
| N8 | the interactive configuration step runs on the pre-rollout runtime and writes beside the current files, validated with the landed code (§12.4) | settled |
| N9 | this repository's home is named `bro`, after the repository, as ppp's and kap's are — amended in review from `core`, which names the `bro` distribution in this repository's docs | settled |
| N10 | `harness`, `summon-harness` and `summon-depth` are keys of every layer, and the host's top-level copies go (§5.1, K4) | settled |
| N11 | home settings are the least specific layer, below `defaults` (§5.2), so the host overrides what a home ships for every key; today a repository's `harness` beats the host's while the host's `summon-depth` beats the repository's | settled |
| N12 | home settings may carry `llm`, so a home ships recipes for its harnesses; bro layers are the host's only (§5.1) | settled |
| N13 | homes keyed by name retire configuration that differs between hosts sharing one `~/.bro.json`, which the owner's file does not hold (K5, §17) | settled |
| N14 | the recipe grammar has no whitespace and no `+` or `,` in a model id; fields are typed by their declarations and unset by omission; the canonical text omits default values; field types are restricted and every value is writable, so every recipe has canonical text (§3.1 to §3.5, R5, R6) | settled |
| N15 | `dump` and `from_dict` are defined once over the recipe class's fields, and a dump missing a field loads its default (§3.4) | settled |
| N16 | the resume record keeps the resolved recipe and its source, `ride resume` takes `--llm`, older records are recognized and refused, and a recorded recipe or a native replacement is checked before the session starts (§8) | settled |
| N17 | the banner shows the source beside the recipe, rendered from its run's own record (§9) | settled |
| N18 | `passthrough` stays an open authority option (§6.3) | settled |
| N19 | `bro.native.llm` takes a required `--llm` (S4) | settled |
| N20 | recipes under a harness the installation lacks are checked for grammar only (V2) | settled |
| N21 | `bro run` and `bro chat` resolve through the managed stack (S2) | settled |
| N22 | the code names the concept a recipe: `Recipe`, `recipe`, `resolved_recipe`, `RIDE_RESOLVED_RECIPE`, `--resolved-recipe` (§11) — amended in review from `RIDE_RECIPE`, so every carrier of the record has one name | settled |
| N23 | one resolved-recipe record of recipe and source travels everywhere (§7) | settled |
| N24 | `RETIRED_MODELS` is a class attribute beside `MODEL_INPUTS` (§3.5) | settled |
| N25 | the rollout rewrites `~/.bro.json` in place, so ppp, kap and the second host can neither launch nor read a credential from the rewrite until each moves past this design, which the rollout schedules (§12.2, §12.3) | settled |
| N26 | every launch, of any worker type, needs an allowance of at least 1, as today's summon depth does (D1) | settled |
| N27 | `bro chat --fork` without `--llm` runs the trail's recorded recipe, as `ride resume` runs the recorded one (S3) | settled |
| N28 | ride names project images `bro/ride-project:<hash>`, labeled with their attachment, and `image-repository` goes (P2, P3) | settled |
| N29 | `ride banner`'s plain-lines switch is `--plain` (§9) | settled |
| N30 | the default bro's key is `default-bro`, set by home settings or `homes.<home>` (K1) | settled |
| N31 | a source is a layer's name, `<home> home settings` for home settings, `--llm` for every command's flag, `summon llm` for a summon's field, and `trail <id>` for a fork's recorded recipe (§2, §6.2) | settled |
| N32 | a manual summon's commit runs on the operator's `--harness`, else the child's stack's `harness`, as today (§5.3) | settled |
| N33 | a benchmark trial carries its preset's harness and recipe in a host configuration it writes into its task container, so summons inside the trial resolve (S6) | settled |

## 15. Implementation mapping

- `bro/llm/llm.py` — the base class of §3.5, renamed `Recipe`: the required `model`, generic `dump` and `from_dict`, R6, `needed_secrets` as a class method, `admitted` without the deferred clause;
  `EFFORT_LEVELS`, `selection_fields`, `deferred_fields`, `effort_values`, `selection`, `with_selection`, `fast` and `with_effort` go.
- `bro/llm/providers.py` — `parse_recipe`, `bind_recipe`, `format_recipe`, `require_admitted` and its message, `RecipeError`;
  declaration validation without `DEFAULT_MODEL` and `MODELS`, with R2, R3 and R5;
  the mechanisms of §11's first rows go.
- `llms/openai`, `llms/deepseek`, `llms/anthropic`, `harnesses/native/harness/bro/native/echo_declaration.py` — `Recipe` with explicit `MODEL_INPUTS` keys and `RETIRED_MODELS`, the fields of §3.5, no `_dump_impl` or `_from_dict_impl`;
  DeepSeek's model-shape check and Anthropic's `DEFAULT_EFFORT` go;
  echo's zero-priced table.
- `wires/responses/bro/wire_responses/client.py`, `wires/chat/bro/wire_chat/client.py`, `harnesses/native/harness/bro/native/echo.py`, `bro/llm/wire.py`, `ride/ride/scope.py` — the recipe's own fields where they read `selection()`.
- `bro/harness/__init__.py`, `ride/ride/harness.py`, `harnesses/native/harness/bro/native/loop.py` — `check_recipe` and `llm_providers` from the declarations' classes;
  `resolve_recipe(base, selection)`, `runs` and `resolve_llm` go.
- `bro/bro.py`, `bro/registry.py` — `llm_spec` leaves the declaration;
  `create_bro` and `missing_secrets` take the run's recipe;
  the registry reads the `bro.bros.<home>` groups, each bro's home and the `bro.settings` entries (H1 to H3, G1 to G3), naming distributions in its refusals;
  the `summon` tool's `llm` description.
- `bro/show.py` — the bro's home; no recipe line.
- `bro/launch/llm_flags.py` — one `--llm` flag; presets and the piece flags go.
- `bro/base/host_config.py` — the schema of §5.1 under `homes`, `default-bro`, a non-negative `summon-depth`, the owned and foreign parts of §5.4;
  `launch_llm`, `launch_selection`, `summon_depth` and `LLMDefault` go.
- `bro/workspace/project.py` — the attached tree's `[tool.bro]` of P1;
  the marker and home-settings readers move to a module of their own.
- `ride/ride/workspace/docker.py`, `ride/ride/workspace/model.py` — project images named, labeled, pruned and looked up as P2 and P3 say.
- `ride/ride/authority_reading.py` — the reading of the marker and the home settings, the layer stack (§5.2), the owned and foreign parts (§5.4);
  per-bro base recipes and the providers' default recipes go.
- `ride/ride/authority_scope.py` — `resolve_recipe` (§6.1), the options of §6.3 without `effort`, `fast` and the `~default` symbol, `checked_recipe` built on them.
- `ride/ride/launch_control.py`, `bro_worker.py` — the allowance (§5.6), recorded on each mission and checked at each launch and manual commit;
  the joined member's command without the piece flags.
- `ride/ride/cli.py`, `flags.py`, `session.py`, `listing.py`, `scope_report.py`, `dive_in.py`, `operator.py` — `--llm` alone;
  the resume record, the recognition of older records, `ride resume --llm` carried through the runtime re-exec, and the checks of §8;
  scope's output (S1);
  `default-bro`;
  the stack in place of attachment identities.
- `ride/ride/runtime_bundle.py` — the manifest's home and the marker its materialization writes (I3).
- `bro/summon.py` — the payload's `llm` alone.
- `bro/workspace/banner.py`, `ride/ride/do_ride.py`, `ride/ride/session.py`, `launch_control.py`, `bro_worker.py` — the banner line rendered from the run's own record, `ride banner --plain`, and the resolved-recipe record of §7 under its new names.
- `harnesses/native/harness/bro/launch/run.py`, `call.py`, `resume.py`, `native/llm.py`, `native/runner.py`, `native/harness.py`, `fork.py` — S2 to S4;
  the runner takes the run's recipe explicitly.
- `harnesses/claude/harness/bro/claude/harness.py`, `claude_argv.py`, `runner.py` — §7, and the recipe the recorder writes.
- `bro/llm/mu.py` — S5.
- `benchmark/bro/benchmark/harbor_agent.py`, `bundle.py`, `trial_store.py`, `retention.py`, `trajectory.py`, `benchmark/agents/*.json` — S6, the presets in recipe text, and the class's `WIRE` read from the declaration in place of `default_spec`;
  the bundle's home in its manifest, its new format and its marker.
- `bro/authority/values.py`, `syntax.py`, `records.py` — `Symbol` and `~` go.
- The five bros declaring a recipe (`browser`, `terminal`, `devoops`, `bro-dev`, `bro-eyebro`) — their `llm_spec` goes,
  and with it each distribution's dependency on a provider plug-in that nothing else in it uses.
- Every `pyproject.toml` registering bros — its `bro` group becomes `bro.bros.bro`;
  `bro-local` — its `bro.settings` entry and `bro-settings.toml`, which `local/`'s list of files a wheel must carry names;
  the root `pyproject.toml` — `[tool.bro]` keeps only `[tool.bro.devoops]`;
  `setup.sh` and `.github/workflows/tests.yml` — the marker line;
  the root `conftest.py` — the fixture setting the installation's home.

## 16. Documentation

- `bro/reference/ride.md` — "Harness selection", "Recipe selection", "Per-project defaults" (renamed for homes), the `ride along` flags, "Harness seam" and "Runtime bundles";
  the Claude argv, the project image, and the lifecycle verbs (`ride resume --llm`, `ride scope`, `ride banner --plain`);
  the banner, `RIDE_RESOLVED_RECIPE` and its record, and the allowance.
- `bro/setup/AGENTS.md`, "Host config", and `bro/base/host_config.py`'s docstring — the schema of §5.
- `bro/reference/llm_providers.md` — the declaration contract (§3.5), recipe text, admission without deferred models, retiring a model.
- `bro/reference/extending.md` — the entry-point groups, "Registering a bro" with `bro.bros.<home>`, "Declaring a bro" without a recipe, home settings, the installation's marker.
- `bro/reference/permissions.md` — the `~default` prose, "Bro" (§6.3), "Readings, bases, and configuration", "Start surfaces", "Manual children" and "Resume and durable records";
  "Auxiliary LLM credentials" (`mu` material in recipe text), and the allowance;
  `bro/reference/permissions_cheatsheet.md` — its `projects` examples, "Run a child on another provider", "Resume with different settings", and the banner switch.
- `bro/reference/mu.md` and `bro/reference/dive_in.md` — `mu` material in recipe text, and `dive-in`'s default bro and `--llm`.
- The entries naming removed or renamed symbols in `bro/AGENTS.md`, `bro/llm/AGENTS.md`, `bro/launch/AGENTS.md`, `bro/base/AGENTS.md`, `bro/workspace/AGENTS.md` and `bro/prompts/AGENTS.md`,
  and in `ride/AGENTS.md`, `harnesses/native/harness/AGENTS.md`, `harnesses/claude/harness/AGENTS.md` and `benchmark/AGENTS.md`.
- `README.md`, `BOOTSTRAP.md`, `benchmark/README.md` — `--llm` in recipe text, `ride scope` without `--repo`,
  and a consumer's groups, home settings, marker and host configuration in place of `projects`, presets, `llm_spec` and `[tool.bro]`'s launch keys.
- `bros/bro/spells/ask.md`, `dev/bros/lead/spells/orchestrate.md`, `dev/bros/eyebro/spells/review-pr.md`, `dev/bro/dev/spells/reflect.md`, `dev/bro/dev/spells/bump-bro.md`
  — launch lines passing `--effort`, `projects.<identity>` entries, and a consumer's move (§12.2).
- The help and refusal texts of `ride/ride/flags.py`, `bro/launch/llm_flags.py`, `ride/ride/authority_scope.py` and `ride/ride/authority_reading.py`.
- Any other page naming a removed symbol, found by grep when the change is made.

## 17. Verified assumptions

No assumption of this design remains unverified.
Checked on 2026-11-01:

- **X1.** Claude Code 2.1.296, run under the setup token with an empty configuration directory, answered under `--model` set to each of the eight listed Anthropic ids, reporting each model's usage;
  its `--effort` takes `low`, `medium`, `high`, `xhigh` and `max`, the values of the Anthropic `effort` field.
- **X2.** Harbor 0.24.0's source (`harbor/agents/base.py`, `harbor/models/job/result.py`):
  `BaseAgent` splits `model_name` once at `/` and keeps the rest as `ModelInfo.name`, a plain string;
  a job keys its results by `<agent>__<model>__<dataset>`, which its CLI splits at `__` only to title its tables, and nothing makes a path of it,
  so `,`, `=` and `+` pass unchanged.
- **X3.** The owner's `~/.bro.json` is shared by two hosts, and both use only URL keys, so no configuration differs between them.
- uv's build backend ships each declared module root whole (`dev/AGENTS.md`, "Wheel contents"),
  and package data reaches a frozen runtime bundle, as this session's `bros/lead/spells/orchestrate.md` shows, so `bro-settings.toml` travels like the spell files (G2).
- `uv sync --all-packages --all-groups --all-extras` keeps a file it did not create at the environment's root, so the marker survives a re-sync that does not recreate the environment (I1);
  a frozen bundle, rebuilt from wheels, carries the marker only through its manifest (I3).
