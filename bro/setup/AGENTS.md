# Setup and credential schemas

How to bring up a fresh checkout, plus what the framework reads out of `~/.bro`.
Run any script with `--help` for flags.

## Setup

A repository operated by `ride` may provide a root `setup.sh` to provision its project environment;
an absent script is logged and skipped.
Session machinery comes from the invoking installation's frozen runtime bundle in both modes, so the project environment need not provide `ride` or `bro`.
A setup script can run `uv sync`, activate the project environment only for its own provisioning process, and call `bro.dev.install`;
managed sessions keep that environment off PATH.
In containers, `RIDE_VENV_MANIFEST` names the optional project's staged manifests so setup can reuse the bake until the tree diverges.

The framework checkout's own bring-up is `./setup.sh` (root `AGENTS.md`, "Development");
its prerequisites are documented in `README.md`.

### Worktrees

An unboxed clone's `setup.sh` may create its `.venv`.
Boxed workspaces receive an optional project dependency bake at `/opt/project-venv`;
setup syncs it once the workspace manifests move away from the staged baseline.
Neither environment enters the session PATH.
Never run `uv sync` against the main checkout from inside another clone:
editable installs record absolute source paths.

## Files

- `setup_env.sh` — reference host-prerequisite installer for macOS and Ubuntu;
  invoked by nothing
- `versions.sh` and `ubuntu/` — pinned host-tool versions and Ubuntu installers used only by `setup_env.sh`
- `uv-version` — packaged uv pin shared by host provisioning and image builds;
  `bump-uv.sh` updates it from PyPI and remains checkout-only
- `prelude.sh` — shell-script prelude every executable framework script sources,
  publishing `HERE`, the executed script's directory;
  consumers resolve the packaged directory with `bro-shell-dir`
- `install_awscli.sh` — packaged macOS/Linux AWS CLI installer
- `log.sh` — leveled shell logging thresholded by `BRO_LOG_LEVEL`
- `strict.sh` — fail-fast shell guards, including command-not-found inside test positions
- `docker_smoke_test.sh` — packaged sourceable helper for service image smoke-test scripts
- `dev/bro/workflow/hooks/post-commit` — packaged by `bro-dev` and installed by `bro.dev.install`;
  it advances token-accounting state after each commit

The managed-session image and its local base-image builder live under `ride/ride/setup/`;
`ride.workspace.build_context` injects them together with the shell helpers above.

## Configuration

Credentials live in one exclusive store:
`BRO_STORE` when set, otherwise `~/.bro`.
The repository carries only credential kinds and their behavior.
A resolver reads the code registry assembled from `bro/base/registry.json` and installed `bro.credentials` entry points;
each entry is `{description, install?}` and unknown fields fail with the valid storage locations named.
Dotfiles cannot add or override kinds.

Material is convention-named:

```text
<store>/creds/<name>.cred
<store>/creds.json
```

`<name>` is a kind (`github`) or instance (`github+reviewer`).
No `creds.json` entry means the material file is plain text.
An entry changes how that name is read without changing the path:

```json
{
  "github+reviewer": {"type": "github_app"},
  "service": {"type": "ssm", "parameter": "/service/credential", "region": "eu-west-1"}
}
```

A minting source reads its config from the convention material path and writes its cache beside it as `<name>.cred.minted`.
An SSM source needs no material file.
There is one source per name and no directory fallback.
Entries whose kinds this installation does not register are skipped so one host store can serve several installations;
malformed entries still fail.

Retired `<store>/registry.json` and `<store>/credentials.json` files fail loudly.
Use `BRO_STORE` for a synthesized service or session store.

The `credentials get <kind>` CLI applies the store's explicit selection, while `--instance` addresses one stored name exactly.
`credentials list` prints every registered kind with its description;
`credentials list --instance` enumerates convention material and typed annotations without resolving them.

An entry's optional `install` hook declares how the secret reaches a tool that reads it from outside the resolver
— declared state, never code to run, so the same hook serves a boxed session and an unboxed session running as the launching user.
Three sections:
`files`, written under the session's install directory at 0600 and named relative to it;
`env`, the variables the launch applies to the session environment;
and `commands`, a tool shadowed by a wrapper first on the session's PATH carrying its own environment per invocation.
A value is text, `{"path": "<relative path>"}` for a path inside that directory, or `{"secret": "<name>"}` for a credential's value
— resolved through the launch's passed store as late as its position allows, so a wrapper re-resolves per invocation and a short-lived minted token is never baked in.
Every string is a template (`bro/reference/template.md`) rendered with `#name` bound to the kind:

```json
{"install": {
  "env": {"AWS_SHARED_CREDENTIALS_FILE": {"path": "aws-credentials"}},
  "files": {"aws-credentials": {"secret": "{{insert #name}}"}}
}}
```

### Host config (`~/.bro.json`)

The optional host config selects stored credential instances and layers session scope policy per consumer:

```json
{
  "defaults": {
    "creds": ["github+dev", "trails+write"],
    "grant": [":bro.party.join"]
  },
  "projects": {
    "https://github.com/me/bro": {
      "creds": ["brog+github", "github+dev"],
      "grant": ["@reviewer"],
      "bros": {
        "bro-eyebro": {"creds": ["github+reviewer"], "revoke": [":bro.party.join"]},
        "eyebro": {"creds": ["github+reviewer"], "grant": ["github"], "llm": "openai:sol:xhigh"}
      }
    },
    "/home/me/projects/bro": {
      "creds": ["aws+laptop"],
      "grant": [":bro.party.start.unboxed"],
      "revoke": [":bro.party.start.boxed"]
    }
  },
  "user": {
    "creds": ["github+me"],
    "tools": {"bro.trails.rewind": {"creds": ["trails+analyst"]}}
  },
  "llm": {"sharp": "openai:sol:max"},
  "summon-depth": 4
}
```

Every selection list is named `creds`.
An entry is `kind+instance`, its instance left empty (`kind+`) to select the kind's empty instance;
one list may name a kind once.
`defaults`, each project entry, and each `bros.<bro>` entry may also carry `grant` and `revoke` in the full launch-scope grammar:
a bare credential kind changes whether the scope holds it, `@bro` changes the summon allow-list, and a `:permit` leaf changes party authority.
Credential grants and revokes cannot name instances;
use `creds` to pick an instance and add a bare grant only when the consumer does not already need the kind.
Worker permits have the form `:<type>.<leaf>`, with one or more dot-separated leaf segments.
The bro type declares `:bro.party.start.boxed`, `:bro.party.start.unboxed`, and `:bro.party.join`, while the webview type declares `:webview.vnc`;
`:bro` is malformed and `:bro.party` names an undeclared leaf rather than expanding to its descendants.
A bro's `creds` selects only among the kinds its configured scope holds;
a selection of any other kind fails the launch and names `grant`, since it would otherwise sit inert.
The same entry may pick and grant a kind.
A `bros` entry may also carry `llm`, the recipe the bro runs by default on this project, in the `--llm` grammar.
It fills what the launch leaves unnamed:
a launch naming a provider or a model keeps its own pair, an effort or `+fast` it does not name is read from the entry, the path entry fills before the URL entry, and whatever no layer names keeps the bro's declared recipe.
The settled recipe is what the session records and forwards, so a summon of the bro in the project and `ride scope` read the same entry, while `bro run` and `bro chat` attach to no project and read none.
A recipe the selected harness cannot run fails the launch as an explicit `--llm` would, and a malformed one fails the launch that reads it, naming the entry.
The retired `instances` field is rejected with `creds` named as its replacement.
The file read validates credential and bro name grammar without consulting an installation's registries.
Each launch then requires every credential name in every applicable layer to be registered;
this lets one shared file carry consumer-specific names in project entries without letting an inapplicable or misspelled name pass silently.
A recipe is carried as written for the launch to parse.

`defaults.creds` is the root both branches extend.
`user.creds` covers every command the operator runs outside a session, and `user.tools.<command>.creds` narrows that to one of them;
a session instead takes the matching project URL, project path, URL-bro, and path-bro layers in that order.
The project layers' `grant` and `revoke` lists follow the same order after the repository's own `[tool.bro]` layer.
Credential grants and revokes are idempotent at every layer, including launch and resume flags, so a more-specific layer may restate either state.
`@bro` and `:permit` launch overrides retain their strict no-op checks.
The user and session branches are disjoint, so a `user` entry never reaches a session.
A kind no layer selects reads its empty instance.

A session against a checkout carries two identities:
the checkout's path, and its `origin` remote when that remote is a git URL.
Every entry either one names applies, the path entry layering over the URL entry at each of the two project ranks
— so the URL entry holds what follows the repository from machine to machine, and the path entry names only the kinds one machine selects differently.
A `projects` key is therefore portable across the dotfiles a host config is shared through, without a per-machine copy of the selection beside it.
An `origin` naming a local path contributes no identity:
it names the checkout this one was cloned from.

A `user.tools` key is the command's canonical console-script name — its import path with the underscores dashed (`bro.trails.rewind`), the name `sync-scripts` publishes every CLI under beside its bare alias.
The alias is what any distribution may claim, so keying on it would let two commands answer to one entry;
an entry keyed by the alias of the running command fails the read rather than sitting inert.

A launch whose attachment no project entry names simply reads the layers that do apply, ending at the kind's own stored material.
A launch can override its computed selection with `--cred kind+instance` without adding the kind.

Every console script names its module to `bro.base.args.run_cli`, which records the canonical name of the command the process is.
On first credential access, an ambient resolver reads the host config and applies `defaults`, `user`, and that command's own layer.
Parsing a CLI that never accesses a credential does not read the host config.
When `BRO_STORE` is set, the resolver does not consult the host config at all:
a session or service store is already the product of a selection.

The `llm` table remains the host-wide recipe presets layered over project defaults.
`summon-depth` is a host-wide positive integer overriding the attached repository's `[tool.bro] summon-depth` value.
When both omit it, launches use the framework default;
detached launches have no project value and therefore use the host value or that default.
`bro/base/host_config.py` owns and validates the schema.

A JSON credential may reference another credential:
`{"$cred": "<name>"}` resolves to its value, and `{"$cred": "<name>", "field": "<key>"}` selects one top-level field.
A kind target applies the reading store's selection;
an instance target reads that stored name directly.
A cacheable expansion is embedded into a scoped store.
When a chain reaches a minting source, the referrer ships with references intact and each referenced kind is hydrated transitively so the session can mint fresh values.

Common material paths and shapes:

- `creds/brog.cred` — task-tracker backend selection.
  The built-in GitHub backend accepts `{"backend": "github", "token": ..., "repo": "owner/name"?}`;
  omitting `repo` derives it from the workspace's origin remote.
- `creds/trails.cred` — trail storage selection.
  Absent means local storage;
  `{"backend": "service", "base_url": ..., "token": ...}` selects the service, and the DynamoDB/S3 shape belongs to a trails-server scope.
- `creds/trails_tokens.cred` — tokens accepted by a trails server.
- `creds/openai.cred`, `creds/anthropic.cred`, and `creds/brave.cred` — JSON objects carrying each service's `api_key`.
- `creds/claude_code.cred` — the scalar long-lived OAuth token from `claude setup-token`.
  The `claude_code` install hook exports it as `CLAUDE_CODE_OAUTH_TOKEN`.
- `creds/aws.cred` — the AWS shared-credentials file installed for SDK and CLI consumers.
- `creds/github+<instance>.cred` — a GitHub App config such as `{"app_id": ..., "installation_id": ..., "private_key": "<PEM>"}` when the matching `creds.json` entry is `{"github+<instance>": {"type": "github_app"}}`.
  Resolution mints an installation token and holds it at `creds/github+<instance>.cred.minted`.

**Scoped stores.**
A managed session reads a synthesized store rather than the host's, in this same layout, and the directory is the bound:
a name not hydrated resolves to `SecretNotFound`, while the code registry's full kind universe remains known for capability checks.
The host skips only an optional kind whose empty instance is both unpicked and absent;
every other held name must be present and load successfully before launch.
How a launch selects, hydrates, and delivers it, install hooks included:
`bro/reference/ride.md`, "Scoped credential hydration".
