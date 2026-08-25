# bro-oops

`bro-oops` is the consumer-neutral deployment and operations member of the bro workspace.
It packages reusable shell machinery without adding deployment dependencies to the core or development distributions.

## Shell assets

Install `bro-oops` in the repository that owns a deployment, then resolve the packaged asset directory at runtime:

```bash
source "$(bro-oops-dir)/deploy_lib.sh"
```

`deploy_lib.sh` accepts region, repository, CodeBuild project, and CDK-directory values from its caller.
The caller resolves those values from its infrastructure config before invoking the helpers.
`monitor_ecs.sh` follows the same boundary by taking region, cluster, and service as arguments.

The packaged `buildspec.yml` is a CodeBuild pattern for consumer repositories.
The project supplies `TARGET` and a checkout-relative `IMAGE_BUILD_SCRIPT` environment variable;
the script can source `deploy_lib.sh` through `bro-oops-dir` after `uv sync` installs the member.

Framework-wheel staging builds the current working tree when run from the bro framework checkout.
In any other checkout it builds the exact bro revision resolved by the consumer's `uv.lock`.

## Development

The root repository owns formatting, lint, typing, packaging policy, and the test gate.
Build this member with `uv build --package bro-oops`;
regenerate its scripts and committed `bro/oops/_entrypoints.py` with `sync-scripts --project oops`.
