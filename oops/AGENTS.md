# bro-oops

`oops/` is the `bro-oops` uv workspace member.
It owns reusable deployment and operations machinery while account and service configuration stays with the consuming repository.
The root repository owns formatting, lint, typing, packaging policy, and the test gate.

## Components

- `bro/oops/assets.py` — validates and locates the shell assets shipped in the wheel
- `bro/oops/infra/deploy_lib.sh` — sourceable image-build, ECR, CodeBuild, wheel-staging, and CDK helpers
- `bro/oops/infra/monitor_ecs.sh` — ECS deployment-state monitor
- `bro/oops/infra/buildspec.yml` — consumer-configured CodeBuild image-build pattern
- `bro/oops/infra/server_base/` — local-only base image for Python services

Build the member with `uv build --package bro-oops`.
Regenerate its console scripts and committed bridge with `sync-scripts --project oops`.
