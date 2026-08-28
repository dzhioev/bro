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

## Operations

The `devoops` persona mounts the `infra` toolset for deploy, verify, restart, ECS status, and HTTP probe operations.
It adds brog task tools only when a `brog` credential resolves, so the same persona works with any contributed brog backend.
Its credential manifest is the union of the active target registry, the optional brog component, and the selected harness tools.

Each consuming repository points `[tool.bro.devoops].target-registry` at a `module:attribute` holding a `TargetRegistry`.
The registry declares its credential requirements and lazily produces named `DeployTarget` values with repository-relative deploy and verification commands.
Targets may also declare ECS coordinates, changed-path prefixes, and an HTTP probe.
Probe authentication is data on the probe:
a fixed header value or an SSM parameter-backed header can be selected without target-specific tool code.

This repository's registry is `oops.deploy_targets:registry`.
Its `trails-server` target resolves region, cluster, service, and URL from the `infra` credential and runs `oops/trails/server/deploy.sh` and `oops/trails/server/verify.sh`.

## CDK constructs

`bro.oops.cdk.resolve()` reads the `infra` credential and applies the package's consumer-neutral defaults.
Deployment-specific overrides live under the credential's `oops` object;
other top-level fields remain available to the consuming repository.
The typed configuration carries the region, delegated subdomain, platform names and construct ids, ECR stack definitions, image-build source and names, and trails resource names.
The trails security-group rule logical ids are configurable because CDK derives them from the platform construct path;
existing stacks set their current ids while new deployments use neutral defaults.
Unknown fields inside `oops` fail resolution so a misspelled live-resource name cannot silently select a default.

`PlatformStack` creates the shared VPC, ECS cluster, ALB, HTTPS listener, wildcard certificate, and Route 53 lookup.
Its account-specific construct ids are configuration because CloudFormation logical ids must remain stable when an existing stack changes CDK apps.
Tests can pass `HostedZoneReference` to synthesize without an AWS context lookup.

Service stacks take the platform's VPC, cluster, hosted zone, load balancer, and HTTPS listener as a `PlatformHandles` value.
The app hands them `PlatformStack.handles`, so CloudFormation carries the platform's identifiers as stack exports;
resolving them to literals instead rewrites immutable properties such as `VpcId`, `Cluster`, and `ListenerArn`, which CloudFormation treats as a change even when the value is unchanged and answers by replacing the service.
A service assertion test injects its own `PlatformHandles` fixture.

`RepositoryStack` creates one configured ECR repository while preserving its configured construct id.
`ImageBuildStack` creates the configured CodeBuild project, grants it pushes to every configured repository, and reads the checkout-relative buildspec and image-build script paths from the same credential.
A private source needs a GitHub identity, named by `connection_arn`;
without one the project builds a public source unauthenticated, with build-status reporting off.
The project names its connection through the source's `Auth` block rather than through a CodeBuild source credential, which is a single default per account and region and so cannot serve two image-build stacks.
The connection is a prerequisite rather than a stack resource:
CodeConnections creates one in `PENDING`, a human completes the provider handshake in its console, and CodeBuild refuses a project that references a connection which is not yet `AVAILABLE`.

## Trails service

`bro.oops.cdk.TrailsServerStack` owns the retained DynamoDB tables and S3 spillover bucket, the store-config parameter, the Fargate service, ALB rule, and DNS record.
Its DynamoDB table, key, and index declarations come from `bro.trails.server.dynamo`.
Assertion tests inject handles and never query AWS.

The repository app is `deployment/app.py`, and `trails/server/deploy.sh` deploys its stacks in dependency order.
`trails/server/bootstrap.sh` creates the runtime token parameter, `run_local.sh` serves a local store, `verify_image.sh` smoke-tests the image, and `verify.sh` monitors the ECS rollout before probing health.
The image uses the shared `bro-server-base`, and `image_build.sh` stages the framework wheel through `deploy_lib.sh` before pushing both commit and latest tags.

## Development

The root repository owns formatting, lint, typing, packaging policy, and the test gate.
Build this member with `uv build --package bro-oops`;
regenerate its scripts and committed `bro/oops/_entrypoints.py` with `sync-scripts --project oops`.
