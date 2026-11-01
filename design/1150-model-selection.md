# Design: model selection — complete recipes from configuration, one resolution

This file is the specification of the refactor that [#1150](https://github.com/dzhioev/bro/issues/1150) carries.
It lives on the branch `design/1150-model-selection`, which is never merged:
the pull request that carries it is a design review only.
It starts from the decisions recorded under `## Step 2 outcome` on #1150, from the discussion that followed them, and from #1143 as it landed (`5431fdd2`);
the decision record (§14) lists every decision with its status.

## 1. Goal, scope and constraints

Goal: anyone can say which model a session runs, and why.
A recipe is always complete and written in one syntax;
it comes either from configuration or from an explicit `--llm`;
one function resolves it on every surface;
and the session shows the recipe and the configuration entry it came from.

What it enables, for example:

- In this repository's installation, `ride along bro-dev` on the native harness runs `openai:gpt-6.1-sol,reasoning_effort=high`, because `homes.core.bros.bro-dev.llm.bro` in `~/.bro.json` names it,
  and the banner says `llm: openai:gpt-6.1-sol,reasoning_effort=high (homes.core.bros.bro-dev)`.
- The same bro on the Claude harness runs `anthropic:claude-opus-5-5,effort=xhigh` from `defaults.llm.claude`,
  and Claude Code is started with `--model claude-opus-5-5 --effort xhigh`.
- `browser`, summoned from ppp's installation, reads its credentials and recipes from `homes.core.bros.browser`, its home's entry, unless `homes.ppp.bros.browser` overrides them.
- `ride along --llm openai:gpt-6.1-sol,reasoning_effort=max eyebro` runs exactly that recipe;
  `--llm :sol` and `--llm openai` are refused as incomplete.
- `terminal` keeps its context bound through configuration: `openai:gpt-5.6-terra,reasoning_effort=high,compact_threshold=200000`.
- A malformed entry under `homes.ppp` cannot stop a launch from this repository's installation.

In scope:
the recipe representation (syntax, fields, flags, records) and the provider declarations' tables;
homes: how a bro belongs to one, how a home ships settings, and how an installation names its own;
the host configuration's schema, layering and validation;
the one resolution function and every surface that calls it;
the summon-depth allowance;
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
  this repository's home is `core`, ppp's is `ppp`.
- **A bro's home** — the home whose entry-point group registers it (§4.1): `browser`'s home is `core`.
- **Home settings** — the settings file a home ships in one of its distributions (§4.2).
- **The installation's home** — the home an installation names in its marker file (§4.3).
  Its home settings and its section of the host configuration apply to every bro the installation launches.
- **Host configuration** — `~/.bro.json`, the host's own layers (§5).
- **Layer** — one object of configuration in the schema of §5.1: home settings, `defaults`, `homes.<home>`, or `homes.<home>.bros.<bro>`.
  A layer's **path** names it: `homes.core.bros.bro-dev`.
- **Foreign entry** — `homes.<home>.bros.<bro>` for a bro whose home is not the installation's home (§5.4).
- **Layer stack** — the ordered layers that apply to one bro launched from one installation (§5.2).
- **Reading** — the snapshot of the installation, its home settings and the host configuration that a root takes at its start (`ride/ride/authority_reading.py`);
  its summons reuse it (§6.4).
- **Resolved recipe** — the recipe a start admits (§6.1), together with its **source**: the path of the layer that named it, or the launch surface that passed it.
  One record carries both everywhere (§7): `{"recipe": <the recipe's dump>, "source": <the source>}`.
- **Allowance** — how many more generations of summons a bro may start below itself, lowered by its **cap** (§5.6).

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

`Recipe.dump()` is defined once in the base class:
`{"type": <provider>, "model": <model>, <field>: <value> for every field}`, plus `"passthrough": true` when set.
`Recipe.from_dict(data)` builds the named provider's class from those keys and refuses any other key.
Providers no longer implement `_dump_impl` and `_from_dict_impl`.
The dumps of OpenAI, DeepSeek and echo recipes keep their keys;
an Anthropic dump always carries a model, since the class no longer takes `None` (§3.6).

### 3.5 Recipe classes and their tables

A provider's declaration module exports `Recipe`, `FAILURE_SIGNATURES`, `price`, `PRICE_TABLE`, `PRICE_TABLE_SHA256`, `price_table_from_content` where it persists tables, and `MODEL_SLUG` with `model_family` where it has them.
It no longer exports `DEFAULT_MODEL`, `MODELS` or `MODEL_ID`.

The recipe class declares `TYPE`, `WIRE`, `ENDPOINT` and `MODEL_INPUTS` as today, and `RETIRED_MODELS` beside `MODEL_INPUTS`, so every per-model fact sits on the class;
it declares `model: str` with no default as its first field, and its provider fields, each with a default.
The base class no longer declares `selection_fields`, `deferred_fields`, `effort_values`, `selection()`, `with_selection()`, `fast()` or `with_effort()`, and `EFFORT_LEVELS` leaves core.

- **R1.** A recipe is admitted when it carries `passthrough` or its model is a key of `MODEL_INPUTS`.
  `providers.require_admitted` checks it at `bind_recipe` and at `wires.open`, the two points #1143 enforces it at, with `bind_recipe` in place of `providers.resolve`.
- **R2.** The class's `RETIRED_MODELS` maps each retired model id to its retirement date, the day the framework stopped admitting it.
- **R3.** The models of the price table are exactly the listed models together with the retired ones, and no model is both listed and retired.
  Loading a declaration refuses a declaration that breaks R2 or R3, as it refuses a malformed table.
- **R4.** Refusing a retired model without `+passthrough` names its retirement date:
  `gpt-5.5 was retired on 2026-11-01; listed openai models: gpt-6.1-sol, gpt-6-luna, gpt-6-astra, gpt-5.6-terra, gpt-5.6-luna; add +passthrough to run it anyway`.

The models the price tables hold today without listing them were admitted verbatim until #1143 landed on 2026-11-01, so each gets that date.
A model delisted later records its own.

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

## 4. Homes

### 4.1 A bro's home

- **H1.** A distribution registers bros in entry-point groups named `bro.bros.<home>`, one entry per bro, each pointing at the bro's class.
  The `bro` group goes.
- **H2.** A bro's home is the `<home>` of the group that registers it.
  A distribution may register bros of several homes.
- **H3.** The registry reads the groups from installed metadata without importing any bro module, as it reads the `bro` group today,
  and still refuses two entries claiming one bro name, in any homes.

```toml
# dev/pyproject.toml
[project.entry-points."bro.bros.core"]
dev = "bros.dev:Dev"
eyebro = "bros.eyebro:Eyebro"
lead = "bros.lead:Lead"
terminal = "bros.terminal:Terminal"
```

In this repository `bro`, `bro-browser`, `bro-dev`, `bro-local` and `bro-oops` register their bros in `bro.bros.core`.
ppp registers its bros in `bro.bros.ppp`, so `ppp-dev`, subclassing `bro-dev`, has the home `ppp`.

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
[project.entry-points."bro.bros.core"]
bro-dev = "bros.bro_dev:BroDev"
bro-eyebro = "bros.bro_eyebro:BroEyebro"
bro-file-probe = "bros.bro_file_probe:BroFileProbe"
bro-watch-probe = "bros.bro_watch_probe:BroWatchProbe"

[project.entry-points."bro.settings"]
core = "bro.local"
```

```toml
# local/bro/local/bro-settings.toml
default = "bro-dev"
harness = "claude"
summon-harness = "bro"
```

`bro-local` is the only distribution of this repository that ships `core`'s settings, and no other repository installs it,
so `core`'s settings apply only in this repository's own installation.
They take the keys the root `pyproject.toml`'s `[tool.bro]` holds today.

### 4.3 The installation's home

- **I1.** An installation names its home in the marker file `bro-installation-home` at the root of its environment (`sys.prefix`), holding the home's name and a final newline.
- **I2.** A setup script writes the marker directly;
  this repository's `./setup.sh` gains `printf 'core\n' > .venv/bro-installation-home`, and a consumer's setup writes its own home.
- **I3.** A runtime bundle copies the marker when it freezes the installation, so every session and summon of a root sees the root's home;
  in-process `bro run` and `bro chat` read it from the live environment.
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
| `default` | the default bro | yes | — | — | — |
| `harness` | a harness name, for a root launch | yes | yes | yes | yes |
| `summon-harness` | a harness name, for a summon | yes | yes | yes | yes |
| `summon-depth` | a non-negative `int` (§5.6) | yes | yes | yes | yes |
| `llm` | an object from harness name to recipe text | yes | yes | yes | yes |
| `grant`, `revoke`, `limit` | lists in the authority syntax | yes | yes | yes | yes |
| `creds` | `kind+instance` picks | — | — | yes | yes |
| `bros` | an object from bro name to bro layer | — | — | yes | — |

- **K1.** `default` names the bro that `dive-in` and `ride scope` take when none is named;
  only home settings set it.
- **K2.** `creds` selects stored credential instances, which only the host may name, as today.
- **K3.** `llm` maps each harness to the recipe text the bro runs on it:
  `{"claude": "anthropic:claude-opus-5-5,effort=xhigh", "bro": "openai:gpt-5.6-terra,reasoning_effort=medium"}`.
- **K4.** `~/.bro.json`'s top level takes `defaults`, `homes`, `user` and `operator`.
  `user` and `operator` keep their meaning.
  The top-level `harness`, `summon-harness`, `summon-depth` and `llm` go:
  the first three move into `defaults`, and `llm` (presets) leaves.
- **K5.** A key of `homes` is a home's name.
  `projects`, its path and URL keys, and every reading of configuration from the attached repository go.

### 5.2 The layer stack

For a bro `B` whose home is `H`, launched from an installation whose home is `I`, the layer stack is, least specific first:

1. `I`'s home settings;
2. `defaults`;
3. `homes.I`;
4. `homes.H.bros.B`, when `H` differs from `I`;
5. `homes.I.bros.B`.

The launch's own choices then apply over the stack.

- **L1.** For `default`, `harness`, `summon-harness` and `summon-depth`, the last layer setting the key wins;
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

A root launch runs on `--harness` when given, otherwise on the stack's `harness`;
a summon runs on its `harness` field when given, otherwise on the child's stack's `summon-harness`.
With neither, the start is refused:
`no harness for <bro>: pass --harness, or set "harness" in a layer of ~/.bro.json or in the home settings`, with `summon-harness` and `harness` for a summon.
The selected name must be an installed harness.
`ride resume` keeps the recorded harness.

### 5.4 What a launch reads and validates

At a root's start, the reading reads the installation's marker, its home settings and `~/.bro.json`.

- **V1.** The installation owns its home settings, `~/.bro.json`'s top level, `defaults`, and `homes.I`, every bro layer in it included.
  It validates them strictly and in full:
  an unknown key is refused, and so is a value of the wrong shape;
  an unknown bro under `homes.I.bros` and an unregistered credential kind are refused;
  and a recipe under a harness the installation has is bound in full (§3.2), its provider installed, its fields valid and its model admitted.
- **V2.** A recipe under a harness the installation lacks is checked by `parse_recipe` alone, since another installation reading the same file may run it.
- **V3.** Of the rest of `homes`, the reading reads only the foreign entries `homes.H.bros.B` for the installed bros `B` whose home `H` differs from `I`.
  At the start it checks only their shape:
  known keys, value shapes, recipe text by `parse_recipe`, well-formed names.
- **V4.** A foreign entry's value is bound in full only when a start uses it:
  a recipe when it wins under L2, a harness when it wins under L1, a credential pick and the authority entries whenever its bro starts.
  What the installation cannot honor refuses that start alone, naming the layer path and both ways on:
  install the plug-in it needs, or override the value in `homes.I.bros.B`.
- **V5.** A value of `homes.I.bros.B` shadows the foreign entry's value for the same key, which is then never bound.
  A foreign grant does not shadow:
  the installation answers one it cannot honor with a `revoke` in `homes.I.bros.B`.
- **V6.** Nothing else under `homes` is read, so another home's mistakes reach this installation only through the foreign entries of bros it installs, and only at the starts that use them.
- **V7.** A failure under V1 refuses every launch from the installation, naming the layer path and the key:
  `~/.bro.json: homes.core.bros.eyebro.llm.bro: unknown field 'effort' for openai; fields: reasoning_effort, service_tier, compact_threshold`.

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
    "core": {
      "creds": ["brog+github", "github+dev"],
      "bros": {
        "bro-dev": {"llm": {"bro": "openai:gpt-6.1-sol,reasoning_effort=high"}},
        "eyebro": {
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

- **D1.** A bro's allowance is how many more generations of summons may start below it;
  a bro may summon only while its allowance is at least 1.
- **D2.** A bro's **cap** is the `summon-depth` of its bro layers, `homes.H.bros.B` and `homes.I.bros.B`, the last setting it winning, as for every key (L1):
  the installation's own entry overrides a foreign one.
- **D3.** A root's allowance is the `summon-depth` of its stack's home settings, `defaults` and `homes.I`, the last setting it winning, lowered to its cap when it has one.
- **D4.** A summoned bro's allowance is its summoner's allowance minus 1, lowered to its cap when it has one.
  A cap only lowers the allowance a bro receives, never raises it.
- **D5.** With no `summon-depth` anywhere, a root's allowance is 2, today's default.
- **D6.** Each mission records its allowance, and launch control checks it in place of today's `owner.depth + 1 > summon_depth`.

With the file of §5.5, a root of any bro has the allowance 2, its children 1, and their children 0, as today;
`eyebro` has the allowance 0 wherever it starts, so it never summons.

## 6. Resolution

### 6.1 The resolution function

`resolve_recipe(reading, bro, harness, explicit=None)` (`ride/ride/authority_scope.py`) returns the resolved recipe and its source.

1. When `explicit` recipe text is given, it is the text, and the source is the surface that passed it (§6.2).
   Otherwise the text is the winning `llm` entry for the harness under L2, and the source is that layer's path.
   With neither, the start is refused:
   `<bro> has no recipe for harness <harness>: pass --llm, or set llm.<harness> in a layer of ~/.bro.json or in the home settings`.
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
  The managed native session passes `--resolved-recipe`, the resolved-recipe record its launcher wrote (§7), which `bro run` and `bro chat` take verbatim and still check at `wires.open`.
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
- **A2.** The descriptor's `provider`, `model` and `passthrough` are the resolved recipe's, whether it came from the request or from configuration,
  so a cap on any of them refuses the start, a configured default included, and never changes it.
- **A3.** Admission recomputes the resolved recipe and refuses a start whose descriptor names a different provider, model or `passthrough`.

### 6.4 When configuration is read

A root reads the installation's marker, its home settings and `~/.bro.json` once, at its start.
Its summons resolve against that reading, so an edit made later reaches only roots started after it;
`ride override` remains the way to change a running root's authority.

## 7. Into the session

The resolved-recipe record `{"recipe": <dump>, "source": <source>}` has one shape wherever it travels:
the start's `resolved_recipe`, a manual summon's commit, the resume record (E1), and the session's `RIDE_RECIPE`.

- The native harness passes the record to `bro run` or `bro chat` as `--resolved-recipe`, which runs its recipe verbatim.
- The Claude harness passes `--model <model>` always, `--effort <effort>` when the recipe sets `effort`, and `"fastMode": <fast_mode>` in its `--settings`.
  A recipe without `effort` leaves effort to Claude Code, as an unset field leaves it to the vendor (B4).
- The Claude recorder records the recipe in the trail header, as today;
  each call records the model that answered.

## 8. Resume

- **E1.** A session's resume record keeps its resolved-recipe record (§7).
- **E2.** `ride resume` runs the recorded recipe unless `--llm` replaces it;
  a replacement is resolved as on `ride along`, on the recorded harness.
- **E3.** A recorded recipe whose model has since been retired is refused at binding (R4);
  `ride resume --llm` with a listed model, or the same model with `+passthrough`, resumes it.
- **E4.** A record without a resolved recipe, written before this design, is refused, naming the workspace and the two ways on:
  `bro chat <bro> --fork <trail>` for a native session, or a new session.

## 9. What the session and the tools show

- The banner (`bro::banner`, `ride banner`) gains the line `llm: <canonical text> (<source>)`.
- `ride scope` prints the same, or the refusal (S1).
- `bro show` shows each bro's home and no recipe or LLM-key line, since a bro declares no recipe.

## 10. Failure behavior

| condition | raised by | message names | before |
|---|---|---|---|
| recipe text off the grammar, a repeated flag or field, an unknown flag | `parse_recipe` | the text and the part | any provider is loaded |
| an uninstalled provider | `bind_recipe` | the provider, the installed ones | the start |
| an unknown field, a value of the wrong type, an invalid combination | `bind_recipe` | the field, the class's fields or the value's type | the start |
| an unlisted model without `+passthrough` | `bind_recipe`, `wires.open` | the model, the listed ones, `+passthrough`, and the retirement date of a retired model | the start, or the request |
| a recipe the harness does not serve | `Harness.check_recipe` | the harness, the harnesses that serve it | the start |
| no recipe for the harness | `resolve_recipe` | the bro, the harness, where to set one | the start |
| no harness | §5.3 | the bro, where to set one | the start |
| a missing or malformed marker | the reading (I4) | the marker's path, the line to write | every launch |
| two `bro.settings` entries for one home | the reading (G3) | both distributions | every launch |
| a malformed part the installation owns | the reading (V7) | the layer path and key | every launch |
| a foreign entry's value the installation cannot honor | the start using it (V4) | the layer path, the plug-in to install, the override to add | that start |
| two entries claiming one bro name | the registry (H3) | both distributions | the registry is read |
| a declaration breaking R2 or R3 | `providers.declaration` | the model | the provider is used |
| a summon beyond its summoner's allowance | launch control (D6) | the allowance and the layers that set it | the summon |
| a descriptor that differs from the resolved recipe | admission (A3) | the option | the start |
| a resume record without a resolved recipe | `ride resume` (E4) | the workspace and the ways on | the resume |

## 11. What goes

| mechanism | replaced by |
|---|---|
| `LLMSelection`, `over`, `parse`, `parse_with_presets`, `is_preset`, `resolve`, `resolve_model`, `default_spec`, `carriers`, `provider_of_model` | `parse_recipe`, `bind_recipe`, `format_recipe` (§3) |
| `LLMSelectionError` | `RecipeError`, raised by every refusal of §3 |
| the names `LLMSpec`, `llm_spec`, `resolved_llm`, `RIDE_RESOLVED_LLM` and `--resolved-llm` | `Recipe`, `recipe`, `resolved_recipe`, `RIDE_RECIPE` and `--resolved-recipe`, carrying the record of §7; the user-facing `--llm` and the `llm` key keep their names |
| `--provider`, `--model`, `--effort`, `--fast`, `--passthrough` | `--llm` with complete recipe text |
| `[tool.bro.llm]` and the host's top-level `llm` presets | nothing, until presets return |
| `MODELS`, `DEFAULT_MODEL`, `MODEL_ID` | full model ids in recipe text, admitted by `MODEL_INPUTS`; retired ones in `RETIRED_MODELS` |
| `BaseBro.llm_spec` and the five bros' declarations | `llm` in configuration; the run's recipe passed to the runner explicitly |
| `_SESSION_DEFAULT`, the Anthropic class's `model=None`, `deferred_fields`, `default` in host lists, `~default` | a model in every recipe |
| `EFFORT_LEVELS`, `with_effort`, `fast`, `effort_values`, `selection_fields`, `with_selection` | the provider's own fields |
| the host's ordered `llm` lists and first-served selection | `llm` maps keyed by harness (K3, L2) |
| the authority options `effort` and `fast` | none, until authority over the whole recipe |
| the `bro` entry-point group | `bro.bros.<home>` groups (H1) |
| `projects` keyed by path and URL, and `[tool.bro]` read from the attached repository for anything but the tree | `homes` keyed by home name, the installation's marker and home settings (§4, K5) |
| the host's top-level `harness`, `summon-harness`, `summon-depth` | the same keys in layers (§5.1) |
| the root-wide summon depth | the allowance (§5.6) |
| `Harness.resolve_recipe`, `Harness.runs`, `SessionHarness.resolve_llm`, the reading's per-bro base recipes | `Harness.check_recipe`, `resolve_recipe` (§6.1) |
| `canonicalize`, `drop_piece_flags`, `host_config.launch_llm`, `LLMDefault` | nothing: they have no caller outside tests |

## 12. Records, other processes and rollout

### 12.1 Records

- Trail headers keep recording `dump()`.
  Recorded headers stay readable:
  `rewind`, cost and the benchmark read their fields as stored, and only `bro.fork.fork` rebuilds a header's recipe, from native trails, whose recipes always name a model.
  An Anthropic header recorded with `model: null` is never rebuilt.
- Every model a recorded trail ran is listed or retired, so its cost is still priced (R3).
- Resume records gain the resolved-recipe record (E1);
  older ones are refused (E4).
- Benchmark retention keeps the trial's `model_name` as written.

### 12.2 Contracts with other processes

- **`~/.bro.json`** is read by every installation on the host.
  An installation from before this design refuses the new schema, and this design's refuses the old one.
  ppp's and kap's installations cannot launch from the rewrite until they move their framework pin past this design,
  rename their bro groups to `bro.bros.<home>`, and write their marker.
- **The entry-point groups** are read only by the installation that holds them, so the rename has no order beyond each repository's own.
- **`mu` material** is hydrated into a session's store at its start and parsed at each call.
  A session started before the rewrite fails its `mu` calls (cast, summaries, `look`) once the material is rewritten.
- **The benchmark bundle** carries its own runtime, which parses the `--llm` its trial passes and reads its own marker;
  it is rebuilt after the re-sync, and the agent presets' `model_name`s change in the same landing.
- **The summon payload** is read only by processes of the root's runtime bundle, so its `llm`-only shape needs no order.
- **The hosted trails server** decodes responses and reads trail bodies;
  it parses no recipe text, so it is not redeployed.

### 12.3 Rollout order

1. The landing merges.
2. The interactive configuration step (§12.4) writes `~/.bro.json.next` and the new `mu` material beside the current files.
3. Every root on the host ends.
4. The host's installation re-syncs to the landed commit, which writes its marker;
   the two new files replace the current ones with backups kept;
   and the benchmark bundle is rebuilt.
5. Fresh roots start.
6. ppp and kap move their framework pins, rename their groups and write their markers, as their own work.

Rollback restores the two backups and re-syncs the previous commit.

### 12.4 The interactive configuration step

The last step of the implementation plan is attended and needs the host's own files, so the user launches it as a manual, unboxed session before the re-sync, on the runtime the host still runs.

1. It reads what each installed bro does, in this installation and in ppp's and kap's:
   its persona prompt, tools and spells (`bro show`), and the bros it summons.
2. It reads the current `~/.bro.json` and `mu` material.
3. It proposes the new file:
   `defaults` with a recipe per harness;
   for each home, its credential picks and grants carried over from the path and URL entries they sit under today;
   and, for each bro whose work calls for a different model, knob or allowance, a bro layer with the recipe and the reason.
   It proposes the `mu` material's recipes in the new syntax.
4. The user settles every entry.
5. It writes `~/.bro.json.next` and the new `mu` material beside the current files, and validates them with the landed code:
   `uv run -q ride scope --bro <bro> --harness <harness>` for every installed bro and harness, run from the landed checkout under a scratch `HOME` that holds the candidate files.

## 13. Examples

The examples read the file of §5.5 from this repository's installation (`I` = `core`), unless they say otherwise.

1. `ride along bro-dev` with no `--harness`:
   the stack's `harness` is `claude`, from `defaults`;
   the only `claude` entry is in `defaults`, so the recipe is `anthropic:claude-opus-5-5,effort=xhigh` from `defaults`;
   Claude Code starts with `--model claude-opus-5-5 --effort xhigh` and `fastMode` false.
2. A summon of `bro-dev` with no `harness`:
   the child's `summon-harness` is `bro`, from `defaults`;
   `homes.core.bros.bro-dev.llm.bro` wins over `defaults.llm.bro` (L2), giving `openai:gpt-6.1-sol,reasoning_effort=high`.
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
   `openai:gpt-5.6-terra,compact_threshold=200000`, whose unset `reasoning_effort` is left to OpenAI.
8. From ppp's installation (`I` = `ppp`), a summon of `browser`, whose home is `core`:
   the stack is ppp's home settings, `defaults`, `homes.ppp`, `homes.core.bros.browser`, then `homes.ppp.bros.browser`, which the file lacks;
   `browser`'s `creds` come from `homes.core.bros.browser`, and its `bro` recipe from `defaults`.
9. The same summon, with `homes.core.bros.browser.llm.bro` set to `deepseek:deepseek-flash` and no DeepSeek plug-in in ppp's installation:
   ppp's launches proceed, since the foreign entry is only shape-checked at the start (V3);
   the summon of `browser` on `bro` is refused, naming `homes.core.bros.browser.llm.bro`, the `bro-deepseek` plug-in, and `homes.ppp.bros.browser` as the place to override (V4).
   Setting `homes.ppp.bros.browser.llm.bro` shadows the foreign recipe (V5).
10. A typo under `homes.ppp`:
    this repository's installation never reads `homes.ppp` (V6), so its launches proceed;
    every launch from ppp's installation is refused, naming the layer path and key (V7).
11. `homes.core.bros.eyebro.llm.bro` = `openai:gpt-6.1-sol,effort=xhigh`:
    every launch from this repository's installation is refused (V7), since `effort` is not an OpenAI field.
12. A root `lead` summons `bro-dev`, which summons `eyebro`:
    `lead`'s allowance is 2, `bro-dev`'s 1, and `eyebro`'s 0, both as its summoner's allowance minus 1 and as its cap (D4);
    a summon by `eyebro` is refused (D6).
13. `ride resume <workspace>` of a session that ran `openai:gpt-6.1-sol,reasoning_effort=high`:
    it runs the same recipe, whatever the configuration now says;
    `ride resume --llm openai:gpt-6.1-sol,reasoning_effort=max <workspace>` runs that instead.
14. `bro run dev 'hi'`:
    `bro run` resolves on the `bro` harness through the same stack, so `dev` runs `openai:gpt-5.6-terra,reasoning_effort=medium` from `defaults`.
15. An installation whose marker is missing:
    every launch is refused, naming `<environment>/bro-installation-home` and the line to write (I4).
16. Recipe texts:
    `openai:gpt-5.6-terra` binds with every field at its default, and its canonical text is the same;
    `openai:gpt-5.6-terra,service_tier=priority,reasoning_effort=high` binds, and its canonical text is `openai:gpt-5.6-terra,reasoning_effort=high,service_tier=priority`;
    `anthropic:claude-opus-5-5,fast_mode=yes` is refused, since `fast_mode` is a `bool`;
    `openai:gpt-5.6-terra,reasoning_effort=high,reasoning_effort=low` is refused (T2);
    `openai:gpt-5.6-terra+fast` is refused (T3).

## 14. Decision record

The `## Step 2 outcome` of #1150 records the decisions D1 to D7, S1 to S3, C1 to C5 and L1 to L5, all settled by the owner on 2026-10-31;
this design states them as the rules above, with the amendments below.
The decisions below were raised while designing;
"settled" means the owner ruled on them in the discussion of 2026-11-01, and "proposed" means the design proposes them for the review to settle with the owner.

| id | decision | status |
|---|---|---|
| N1 | retired models: `RETIRED_MODELS` with the framework's retirement dates, the price table exactly the listed and retired models, today's unlisted price rows retired on 2026-11-01 (R2 to R4) — amends C5 | settled |
| N2 | "home" names the concept everywhere, "project" and "site" are dropped, and the host configuration's key is `homes` | settled |
| N3 | bros register in `bro.bros.<home>` groups, replacing the `bro` group (H1 to H3) | settled |
| N4 | a home ships settings through a `bro.settings` entry naming the package that holds `bro-settings.toml` (G1 to G4) | settled |
| N5 | the installation names its home in the marker `<environment>/bro-installation-home`, written by setup, with no command and no fallback (I1 to I6) | settled |
| N6 | foreign entries are shape-checked at the start, bound at use, shadowed by the installation's own bro entry, and answered with `revoke` for grants (V3 to V6) | settled |
| N7 | `summon-depth` is an allowance that a bro's cap can only lower, the cap taken from its bro layers by the usual last-wins rule (§5.6) | settled |
| N8 | the interactive configuration step runs on the pre-rollout runtime and writes beside the current files, validated with the landed code (§12.4) | settled |
| N22 | the code names the concept a recipe: `Recipe`, `recipe`, `resolved_recipe`, `RIDE_RECIPE`, `--resolved-recipe` (§11) | settled |
| N23 | one resolved-recipe record of recipe and source travels everywhere (§7) | settled |
| N24 | `RETIRED_MODELS` is a class attribute beside `MODEL_INPUTS` (§3.5) | settled |
| N9 | this repository's home is named `core` | proposed |
| N10 | `harness`, `summon-harness` and `summon-depth` are keys of every layer, and the host's top-level copies go (§5.1, K4) | proposed |
| N11 | home settings are the least specific layer, below `defaults` (§5.2), so the host overrides what a home ships; today a repository's `harness` beats the host's | proposed |
| N12 | home settings may carry `llm`, so a home ships recipes for its harnesses; bro layers are the host's only (§5.1) | proposed |
| N13 | homes keyed by name retire configuration that differs between machines sharing one `~/.bro.json` (K5) | proposed |
| N14 | the recipe grammar has no whitespace and no `+` or `,` in a model id; fields are typed by their declarations and unset by omission; the canonical text omits default values (§3.1 to §3.4) | proposed |
| N15 | `dump` and `from_dict` are defined once over the recipe class's fields (§3.4) | proposed |
| N16 | the resume record keeps the resolved recipe and its source, `ride resume` takes `--llm`, and older records are refused (§8) | proposed |
| N17 | the banner shows the source beside the recipe (§9) | proposed |
| N18 | `passthrough` stays an open authority option (§6.3) | proposed |
| N19 | `bro.native.llm` takes a required `--llm` (S4) | proposed |
| N20 | recipes under a harness the installation lacks are checked for grammar only (V2) | proposed |
| N21 | `bro run` and `bro chat` resolve through the managed stack (S2) | proposed |

## 15. Implementation mapping

- `bro/llm/llm.py` — the base class of §3.5, renamed `Recipe`: the required `model`, generic `dump` and `from_dict`, `admitted` without the deferred clause;
  `EFFORT_LEVELS`, `selection_fields`, `deferred_fields`, `effort_values`, `selection`, `with_selection`, `fast` and `with_effort` go.
- `bro/llm/providers.py` — `parse_recipe`, `bind_recipe`, `format_recipe`, `require_admitted`, `RecipeError`;
  declaration validation without `DEFAULT_MODEL` and `MODELS`, with R2 and R3;
  the mechanisms of §11's first rows go.
- `llms/openai`, `llms/deepseek`, `llms/anthropic`, `harnesses/native/harness/bro/native/echo_declaration.py` — `Recipe` with explicit `MODEL_INPUTS` keys and `RETIRED_MODELS`, the fields of §3.5, no `_dump_impl` or `_from_dict_impl`;
  echo's zero price row.
- `bro/harness/__init__.py`, `ride/ride/harness.py` — `check_recipe`; `resolve_recipe(base, selection)`, `runs` and `resolve_llm` go.
- `bro/bro.py`, `bro/registry.py` — `llm_spec` leaves the declaration;
  `create_bro` and `missing_secrets` take the run's recipe;
  the registry reads the `bro.bros.<home>` groups and each bro's home (H1 to H3), and the `bro.settings` entries (G1 to G3).
- `bro/show.py` — the bro's home; no recipe line.
- `bro/launch/llm_flags.py` — one `--llm` flag; presets and the piece flags go.
- `bro/base/host_config.py` — the schema of §5.1 under `homes`, the owned and foreign parts of §5.4.
- `bro/workspace/project.py` — `[tool.bro]` keeps only the tree's keys, for #1157 to take over;
  the default `image-repository`, `bro/<default bro>`, takes the default bro from the home settings;
  the marker and home-settings readers move to a module of their own.
- `ride/ride/authority_reading.py` — the reading of the marker and the home settings, the layer stack (§5.2), the owned and foreign parts (§5.4);
  per-bro base recipes go.
- `ride/ride/authority_scope.py` — `resolve_recipe` (§6.1), the options of §6.3, `checked_recipe` built on them.
- `ride/ride/launch_control.py`, `bro_worker.py` — the allowance (§5.6) recorded on each mission and checked at each summon.
- `ride/ride/cli.py`, `flags.py`, `session.py`, `scope.py`, `scope_report.py`, `dive_in.py`, `operator.py` — `--llm` alone;
  the resume record and `ride resume --llm` (§8); scope's output (S1); the stack in place of attachment identities.
- `ride/ride/runtime_bundle.py` — the frozen bundle carries the marker (I3).
- `bro/summon.py` — the payload's `llm` alone.
- `bro/workspace/banner.py`, `ride/ride/do_ride.py`, `ride/ride/session.py`, `launch_control.py`, `bro_worker.py` — the banner line, and the resolved-recipe record of §7 under its new names.
- `harnesses/native/harness/bro/launch/run.py`, `call.py`, `native/llm.py`, `native/runner.py`, `native/harness.py`, `fork.py` — S2 to S4;
  the runner takes the run's recipe explicitly.
- `harnesses/claude/harness/bro/claude/harness.py`, `claude_argv.py` — §7.
- `bro/llm/mu.py` — S5.
- `benchmark/bro/benchmark/harbor_agent.py`, `trial_store.py`, `retention.py`, `trajectory.py`, `benchmark/agents/*.json` — S6, the presets in recipe text, and the class's `WIRE` read from the declaration in place of `default_spec`;
  the bundle's marker.
- `bro/authority/values.py`, `syntax.py`, `records.py` — `Symbol` and `~` go.
- The five bros declaring a recipe (`browser`, `terminal`, `devoops`, `bro-dev`, `bro-eyebro`) — their `llm_spec` goes,
  and with it each distribution's dependency on a provider plug-in that nothing else uses.
- Every `pyproject.toml` registering bros — its `bro` group becomes `bro.bros.core`;
  `bro-local` — its `bro.settings` entry and `bro-settings.toml`, which `local/`'s list of files a wheel must carry names;
  the root `pyproject.toml` — `default`, `harness` and `summon-harness` leave `[tool.bro]`;
  `setup.sh` — the marker line.

## 16. Documentation

- `bro/reference/ride.md` — "Harness selection", "Recipe selection", "Per-project defaults" (renamed for homes), the lifecycle verbs (`ride resume --llm`, `ride scope`), the banner, `RIDE_RECIPE` and its record, the summon-depth allowance.
- `bro/setup/AGENTS.md`, "Host config", and `bro/base/host_config.py`'s docstring — the schema of §5.
- `bro/reference/llm_providers.md` — the declaration contract (§3.5), recipe text, admission without deferred models, retiring a model.
- `bro/reference/extending.md` — "Registering a bro" with `bro.bros.<home>`, home settings, the installation's marker, and declaring a bro without a recipe.
- `bro/reference/permissions.md` and `permissions_cheatsheet.md` — the bro option table (§6.3) and the allowance.
- `bro/reference/mu.md` and `permissions.md`, "Auxiliary LLM credentials" — `mu` material in recipe text.
- `bro/llm/AGENTS.md`, `bro/launch/AGENTS.md`, `harnesses/native/harness/AGENTS.md`, `bro/AGENTS.md`, `benchmark/AGENTS.md` — the entries naming removed symbols.
- `README.md`, `BOOTSTRAP.md`, `bros/bro/spells/ask.md`, `dev/bros/lead/spells/orchestrate.md` — `--llm` in recipe text, a consumer's groups, settings and marker, and the launch lines that pass `--effort`.
- Any other page naming a removed symbol, found by grep when the change is made.

## 17. Unverified assumptions

- **X1.** Claude Code 2.1.296 starts under the setup token with `--model` set to each of the eight listed Anthropic ids.
- **X2.** Harbor 0.24.0 carries a `model_name` containing `,` and `=` unchanged:
  read in its source (`harbor/agents/base.py`, `harbor/models/job/result.py`), not run.
- **X3.** The owner's `~/.bro.json` holds no configuration that differs between the machines sharing it through a path key (N13).

Checked on 2026-11-01 and no longer assumed:
uv's build backend ships each declared module root whole (`dev/AGENTS.md`, "Wheel contents"),
and package data reaches a frozen runtime bundle, as this session's `bros/lead/spells/orchestrate.md` shows, so `bro-settings.toml` travels like the spell files (G2);
and `uv sync --all-packages --all-groups --all-extras` kept a file it did not create at the environment's root, so the marker survives a re-sync that does not recreate the environment (I1).
