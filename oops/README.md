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

## CDK constructs

`bro.oops.cdk.resolve()` reads the `infra` credential and applies the package's consumer-neutral defaults.
Deployment-specific overrides live under the credential's `oops` object;
other top-level fields remain available to the consuming repository.
The typed configuration carries the region, delegated subdomain, platform names and construct ids, ECR stack definitions, and image-build source and names.
Unknown fields inside `oops` fail resolution so a misspelled live-resource name cannot silently select a default.

`PlatformStack` creates the shared VPC, ECS cluster, ALB, HTTPS listener, wildcard certificate, and Route 53 lookup.
Its account-specific construct ids are configuration because CloudFormation logical ids must remain stable when an existing stack changes CDK apps.
Tests can pass `HostedZoneReference` to synthesize without an AWS context lookup.

Service stacks consume a `PlatformHandles` value rather than a same-app stack reference.
`PlatformHandles.lookup()` finds the VPC and ALB by their CloudFormation stack tag, the ECS cluster by its configured name, the hosted zone by domain, and the HTTPS listener by port.
A service assertion test should inject `PlatformStack.handles` or a `PlatformHandles` fixture instead of performing those lookups.

`RepositoryStack` creates one configured ECR repository while preserving its configured construct id.
`ImageBuildStack` creates the configured GitHub connection and CodeBuild project and grants pushes to every configured repository.
The project reads the checkout-relative buildspec path from the same credential.

## Development

The root repository owns formatting, lint, typing, packaging policy, and the test gate.
Build this member with `uv build --package bro-oops`;
regenerate its scripts and committed `bro/oops/_entrypoints.py` with `sync-scripts --project oops`.
