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
After syncing, it dispatches installed harness runtime setup through `python -m ride.provisioning`.

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
each entry is `{description, install?, literal?}` and unknown fields fail with the valid storage locations named.
Provider kinds are contributed by their installed provider distributions, not by the core registry.
A literal kind returns its material without expanding `$cred` reference nodes, and scoped-store hydration follows none of them.
Dotfiles cannot add or override kinds.

Material is convention-named:

```text
<store>/creds/<name>.cred
<store>/creds.json
```

`<name>` is a kind (`github`) or instance (`github+reviewer`).
The credential store writer accepts only canonical registered local names, offers a cross-process per-name lock, and atomically writes material at 0600 only while the stored bytes still equal the caller's expected starting value.
`creds.json` holds the store's default picks and typed source annotations:

```json
{
  "defaults": ["github+dev", "trails+write"],
  "sources": {
    "github+reviewer": {"type": "github_app"},
    "service": {"type": "ssm", "parameter": "/service/credential", "region": "eu-west-1"}
  }
}
```

`defaults` uses the `creds` list grammar and names at most one instance of each kind.
The rules for picks shared across installations are in `bro/reference/ride.md`, "Session permissions and credentials".
A caller's picks layer over it, and a kind neither layer picks reads its empty instance.
A name absent from `sources` reads the convention material file as plain text.
A minting source reads its config from that path and writes its cache beside it as `<name>.cred.minted`;
an SSM source needs no material file.
There is one source per name and no directory fallback.
Source entries whose kinds this installation does not register are skipped after validation, so one host store can serve several installations.
Both top-level keys are optional, and any credential annotation outside `sources` fails.

Retired `<store>/registry.json` and `<store>/credentials.json` files fail loudly.
Use `BRO_STORE` for a synthesized service or session store.
A hydrated session's index additionally carries `generation`, `kinds` (the declared hydrated kinds), and `material`, an authoritative stored-name-to-relative-file mapping under `creds/`.
Only mapped names are readable, regardless of other files in the directory.
A credential override advances the generation and may record its `operation` id;
`bro/reference/ride.md` describes publication; `bro/reference/permissions.md` describes operation settlement and recovery.

The `credentials get <kind>` CLI applies the store's explicit selection, while `--instance` addresses one stored name exactly.
`credentials list` prints every registered kind with its description;
`credentials list --instance` enumerates convention material and typed annotations without resolving them.

An entry's optional `install` hook declares how the secret reaches a tool that reads it from outside the resolver
— declared state, never code to run, so the same hook serves a boxed session and an unboxed session running as the launching user.
Three sections:
`files`, written under the session's install directory at 0600 and named relative to it;
`env`, the variables the launch applies to the session environment;
and `commands`, a tool shadowed by a wrapper first on the session's PATH carrying its own environment per invocation.
A value is text, `{"path": "<relative path>"}` for a path inside that directory, or `{"secret": "<kind>"}` for the value of that kind's picked instance
— resolved through the launch's passed store as late as its position allows, so a wrapper re-resolves per invocation and a short-lived minted token is never baked in.
Every string is a template (`bro/reference/template.md`) rendered with `#name` bound to the kind:

```json
{"install": {
  "env": {"AWS_SHARED_CREDENTIALS_FILE": {"path": "aws-credentials"}},
  "files": {"aws-credentials": {"secret": "{{insert #name}}"}}
}}
```

### Host config (`~/.bro.json`)

The optional file configures installations by home, independently of the attached repository:

```json
{
  "defaults": {
    "harness": "claude",
    "summon-harness": "bro",
    "llm": {
      "bro": "openai:gpt-5.6-terra,reasoning_effort=medium",
      "claude": "anthropic:claude-opus-5-5,effort=xhigh"
    }
  },
  "homes": {
    "bro": {
      "creds": ["brog+github", "github+dev"],
      "bros": {
        "bro-eyebro": {
          "creds": ["github+reviewer"],
          "llm": {"bro": "openai:gpt-6.1-sol,reasoning_effort=xhigh"}
        }
      }
    }
  },
  "user": {
    "creds": ["github+me"],
    "tools": {"bro.trails.rewind": {"creds": ["trails+analyst"]}}
  }
}
```

One schema serves home settings, `defaults`, home entries and bro entries:
`harness`, `summon-harness`, non-negative `summon-depth`, `llm` keyed by harness, and authority lists `grant`, `revoke`, `limit`.
`default-bro` belongs only in home settings or `homes.<home>`.
`creds` belongs only in host home and bro entries;
`bros` belongs only in a home entry.
A recipe uses complete provider-native text, `provider:model[+passthrough][,field=value]...`.
The top level accepts only `defaults`, `homes`, `user` and `operator`.

For a bro B of home H, in an installation of home I, layers apply least specific first:
I's home settings, `defaults`, `homes.I`, `homes.H.bros.B` when H differs from I, and `homes.I.bros.B`.
Scalar keys take the last value;
recipes take the last entry per harness, and credential picks the last per kind.
Authority folds each layer's caps before its grants.
A root takes one reading for its whole mission tree;
a resume takes a fresh reading.
[Ride's configuration reference](../reference/ride.md#home-settings) describes resolution and validation.

Every `creds` entry is `kind+instance`, with `kind+` selecting the empty instance.
One list may name a kind once.
Picks select without holding a kind;
a bare kind in `grant` or `revoke` holds or drops it.
Store `creds.json` defaults sit beneath both the session stack and the independent `user` branch.
A kind no layer picks uses its empty instance.
The host-only `operator` takes only `revoke` and `limit`, restricting operator starts.

`user.creds` applies to commands outside sessions;
`user.tools.<command>.creds` overrides it for one canonical console-script name, its import path with underscores dashed.
An entry keyed by the running command's bare alias is refused.
Every console script identifies itself through `bro.base.args.run_cli`.
The ambient credential resolver reads this branch lazily, on first credential access.
When `BRO_STORE` names a synthesized session or service store, the resolver bypasses the host file.

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
- `creds/cookies+<instance>.cred` — a literal, versioned Playwright storage profile passed only to a webview worker
- `creds/aws.cred` — the AWS shared-credentials file installed for SDK and CLI consumers.
- `creds/github+<instance>.cred` — a GitHub App config such as `{"app_id": ..., "installation_id": ..., "private_key": "<PEM>"}` when `creds.json` annotates the stored name under `{"sources": {"github+<instance>": {"type": "github_app"}}}`.
  Resolution mints an installation token and holds it at `creds/github+<instance>.cred.minted`.

**Scoped stores.**
A managed session reads a synthesized store rather than the host's, in this same layout, and the directory is the bound:
a name not hydrated resolves to `SecretNotFound`, while the code registry's full kind universe remains known for capability checks.
The store keeps selected and transitively referenced instances under their own stored names, with its picks under `defaults` and typed annotations under `sources`.
Credential selection, hydration, and delivery, including install hooks, are documented in `bro/reference/ride.md`, "Session permissions and credentials".
Authority and configured picks are documented in `bro/reference/permissions.md`; common setups are in `bro/reference/permissions_cheatsheet.md`.
