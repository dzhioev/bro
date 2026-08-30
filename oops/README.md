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

The `devoops` persona mounts the `infra` toolset for plan, verify, restart, ECS status, and HTTP probe operations.
It adds brog task tools only when a `brog` credential resolves, so the same persona works with any contributed brog backend.
Its credential manifest is the union of the active target registry, the optional brog component, and the selected harness tools.

Each consuming repository points `[tool.bro.devoops].target-registry` at a `module:attribute` holding a `TargetRegistry`.
The registry declares its credential requirements and lazily produces named `DeployTarget` values with repository-relative deploy, plan, and verification commands.
A plan reports what deploying the target would change in the live account, and its exit status carries the verdict:
a change it judges unsafe exits with `bro.oops.targets.PLAN_UNSAFE_EXIT_CODE`, and any other non-zero status means the plan never reached a verdict at all.
The deploy spell runs it before every deploy and stops on either, so a target's plan command is where the veto over a destructive change lives.
A target that declares none has no veto, and the spell stops for the same explicit authorization rather than deploying it unplanned.
Targets may also declare ECS coordinates, changed-path prefixes, and an HTTP probe.
Probe authentication is data on the probe:
a fixed header value or an SSM parameter-backed header can be selected without target-specific tool code.

This repository's registry is `oops.deploy_targets:registry`.
Its `trails-server` target resolves region, cluster, service, and URL from the `infra` credential and runs `oops/trails/server/deploy.sh`, `oops/trails/server/plan.sh`, and `oops/trails/server/verify.sh`.

## CDK constructs

`bro.oops.cdk.config.resolve()` reads the `infra` credential and applies the package's consumer-neutral defaults.
Deployment-specific overrides live under the credential's `oops` object;
other top-level fields remain available to the consuming repository.
The typed configuration carries the region, delegated subdomain, platform stack and cluster names, ECR stack definitions, image-build source and names, and trails resource names.
Unknown fields inside `oops` fail resolution so a misspelled live-resource name cannot silently select a default.

`PlatformStack` creates the shared VPC, ECS cluster, ALB, HTTPS listener, wildcard certificate, and Route 53 lookup.
One app deploys it;
every app carrying a service on that platform finds it instead, so a service and its platform need not live in the same CDK app.
Tests can pass `HostedZoneReference` to synthesize without an AWS context lookup.

Service stacks take the platform's VPC, cluster, hosted zone, load balancer, and HTTPS listener as a `PlatformHandles` value.
`PlatformHandles.lookup()` finds the VPC and ALB by their CloudFormation stack tag, the ECS cluster by its configured name, the hosted zone by domain, and the HTTPS listener by port.
A service assertion test can inject `PlatformStack.handles` or a `PlatformHandles` fixture instead of resolving those lookups.

`RepositoryStack` creates one configured ECR repository while preserving its configured construct id.
`ImageBuildStack` creates the configured CodeBuild project, grants it pushes to every configured repository, and reads the checkout-relative buildspec and image-build script paths from the same credential.
A private source needs a GitHub identity:
`connection_name` has the stack create a connection, `connection_arn` points the project at one that already exists, and neither leaves a public source unauthenticated.
The project names its connection through the source's `Auth` block rather than through a CodeBuild source credential, which is a single default per account and region and so cannot serve two image-build stacks.

## Trails service

`bro.oops.cdk.trails.TrailsServerStack` owns the retained DynamoDB tables and S3 spillover bucket, the store-config parameter, the Fargate service, ALB rule, and DNS record.
Its DynamoDB table, key, and index declarations come from `bro.trails.server.dynamo`.

The repository app is `deployment/app.py`, and `trails/server/deploy.sh` deploys its stacks in dependency order.
`trails/server/bootstrap.sh` creates the runtime token parameter, `run_local.sh` serves a local store, `verify_image.sh` smoke-tests the image, and `verify.sh` monitors the ECS rollout before probing health.
`plan.sh` diffs the stack roster `deployment_config.sh` declares
— the one `deploy.sh` rolls, in the two groups the image build sits between.
`cdk_diff` reports an unsafe plan for a resource the deploy would replace, destroy or orphan,
and for a resource loop the CLI renders without stating any impact.
It asks for the verdict CloudFormation itself gives, so a plan creates and deletes a change set on each stack through the deploy role rather than only reading the account.
The CLI's rendered text puts a logical id and an impact in the same field, so a resource named for one reads as carrying it and the scan errs toward reporting unsafe.
It runs before the image build, and the service stack synthesizes its image digest from the repository's `latest` tag, so the diff carries structural change alone rather than the task-definition churn every image roll causes.
The image uses the shared `bro-server-base`, and `image_build.sh` stages the framework wheel through `deploy_lib.sh` before pushing both commit and latest tags.

## Development

The root repository owns formatting, lint, typing, packaging policy, and the test gate.
Build this member with `uv build --package bro-oops`;
regenerate its scripts and committed `bro/oops/_entrypoints.py` with `sync-scripts --project oops`.
