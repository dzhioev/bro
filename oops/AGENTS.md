# bro-oops

`oops/` is the `bro-oops` uv workspace member.
It owns reusable deployment and operations machinery while account and service configuration stays with the consuming repository.
The root repository owns formatting, lint, typing, packaging policy, and the test gate.

## Components

- `bro/oops/assets.py` — validates and locates the shell assets shipped in the wheel
- `bro/oops/targets.py` — typed deploy-target declarations and the per-repository registry loader
- `bro/oops/mcp.py` — `infra` MCP operations over the repository's registered targets
- `bros/devoops/` — the operations persona and its target-driven deploy spell
- `bro/oops/cdk/config.py` — resolves the `infra` credential into typed deployment configuration and neutral defaults
- `bro/oops/cdk/platform.py` — shared VPC, ECS cluster, ALB, certificate, and hosted-zone platform plus the lookup handles service stacks consume
- `bro/oops/cdk/ecr.py` — parameterized ECR repository stack
- `bro/oops/cdk/image_build.py` — parameterized CodeBuild image-build stack
- `bro/oops/cdk/trails.py` — retained trails storage and lookup-based Fargate service stack
- `bro/oops/cdk/app.py` — testable assembly for the repository, image-build, and trails stacks
- `bro/oops/infra/deploy_lib.sh` — sourceable image-build, ECR, CodeBuild, wheel-staging, and CDK helpers
- `bro/oops/infra/monitor_ecs.sh` — ECS deployment-state monitor
- `bro/oops/infra/buildspec.yml` — consumer-configured CodeBuild image-build pattern
- `bro/oops/infra/server_base/` — local-only base image for Python services
- `deployment/app.py` — this repository's CDK entry point
- `trails/server/` — trails image, credential registry, deployment, bootstrap, local-run, and verification scripts
- `image_build.sh` — the trails target used by the shared CodeBuild buildspec
- `deploy_targets.py` — this repository's trails-server operations declaration

Build the member with `uv build --package bro-oops`.
Regenerate its console scripts and committed bridge with `sync-scripts --project oops`.
