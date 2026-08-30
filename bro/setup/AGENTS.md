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

The framework repository is a uv workspace whose root publishes `bro`;
`native/` publishes `bro-native`,
`dev/` publishes `bro-dev`,
`ride/` publishes `bro-ride`,
and `local/` publishes this checkout's `bro-local` persona and scripts.
`uv sync --all-packages --all-groups --all-extras` creates the root `.venv`, installs all five editably, and registers each distribution's committed console-script bridge.
The root owns the tool configuration and development gate for every member.

Prerequisites are documented in `README.md`.
`setup_env.sh` remains an optional macOS/Ubuntu reference installer and is not invoked by repository provisioning.

### Worktrees

A host worktree's `setup.sh` may create its `.venv`.
Container workspaces receive an optional project dependency bake at `/opt/project-venv`;
setup syncs it once the workspace manifests move away from the staged baseline.
Neither environment enters the session PATH.
Never run `uv sync` against the main checkout from inside another worktree:
editable installs record absolute source paths.

## Files

- `setup_env.sh` — reference host-prerequisite installer for macOS and Ubuntu;
  invoked by nothing
- `versions.sh` and `ubuntu/` — pinned host-tool versions and Ubuntu installers used only by `setup_env.sh`
- `prelude.sh` — shell-script prelude every executable framework script sources;
  consumers resolve the packaged directory with `bro-shell-dir`
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
— declared state, never code to run, so the same hook serves a container and a host session running as the operator.
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

The optional host config selects stored credential instances per consumer:

```json
{
  "defaults": {"creds": ["github+dev", "trails+write"]},
  "projects": {
    "/home/me/projects/bro": {
      "creds": ["brog+github", "github+dev"],
      "bros": {"bro-eyebro": {"creds": ["github+reviewer"]}}
    }
  },
  "tools": {"rewind": {"creds": ["trails+analyst"]}},
  "llm": {"sharp": "openai:sol:max"}
}
```

Every selection list is named `creds`.
An entry is `kind+instance`, or `kind+` to select the kind's bare `creds/<kind>.cred` material;
one list may name a kind once.
The retired `instances` field is rejected with `creds` named as its replacement.
Validation is grammar-only, so shared dotfiles may carry kinds an installation does not register.

`defaults.creds` applies host-wide.
A matching `projects.<attachment>.creds` layer overrides it for a checkout path or normalized git URL,
and `projects.<attachment>.bros.<bro>.creds` overrides that for one exact bro name.
A host CLI instead applies `tools.<cli>.creds` over defaults, keyed by its console-script basename.
Launch and tool layers are disjoint.
Most-specific precedence is launch flag, project-bro, project, tool for a host CLI, defaults, then bare material.

A launch whose attachment no project entry names simply reads the layers that do apply, ending at the kind's own stored material.
A launch can still override its computed selection with `--grant kind+instance`.

Every framework CLI records its own basename while parsing arguments.
On first credential access, an ambient resolver reads the host config and applies defaults plus that CLI's tool layer.
Parsing a CLI that never accesses a credential does not read the host config.
When `BRO_STORE` is set, the resolver does not consult the host config at all:
a session or service store is already the product of a selection.

The `llm` table remains the host-wide recipe presets layered over project defaults.
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

**Scoped per-bro hydration.**
Managed sessions receive a synthesized store rather than the host store.
A selected instance materializes under its kind as `creds/<kind>.cred`, and generated `creds.json` contains typed-source annotations only.
The store directory itself is the bound:
anything not hydrated resolves to `SecretNotFound`, while the code registry's full kind universe remains known for capability checks.
Container launches pack `.bro/`, its `creds/` directory, material, and `creds.json` into an in-memory tar.
Host sessions materialize the same layout under the workspace state and set `BRO_STORE` to it.
Required credentials fail hydration strictly;
optional credentials are skipped when absent.
Install hooks come from the code registry and receive the explicit hydrated declared-kind set, excluding transitive reference pulls.
Full mechanics:
`bro/reference/ride.md` ("Scoped credential hydration").
