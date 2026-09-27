# bro-oops CDK constructs

`oops/cdk/` is the `bro-oops-cdk` distribution:
the AWS CDK stacks `bro-oops` deploys, as the package `bro.oops.cdk`, a portion of the `bro.oops` namespace `bro-oops` also ships into.
What the constructs are is `README.md`.

## Development

This is not a uv workspace member:
a runtime bundle freezes the workspace venv whole, and `aws-cdk-lib` is most of a bundle while no session imports it.
The directory therefore locks and syncs on its own:
`uv sync --directory oops/cdk --all-groups` builds `.venv` from the `uv.lock` committed beside this file.
It depends through editable path sources on core and on `bro-oops`, whose `bro.oops.config` the stacks read their names from.

Formatting and linting stay the repository root's, which walks this directory through its `[tool.ruff] src`.
pytest and pyright run from here instead, inside `.venv`:
`run-tests` syncs it and drives both in its `cdk` stage.
Build the wheel with `uv build` from this directory rather than `uv build --package`.

## Components

- `bro/oops/cdk/platform.py` — shared VPC, ECS cluster, ALB, certificate, and hosted-zone platform plus the lookup handles service stacks consume
- `bro/oops/cdk/ecr.py` — parameterized ECR repository stack
- `bro/oops/cdk/image_build.py` — parameterized CodeBuild image-build stack
- `bro/oops/cdk/trails.py` — retained trails storage and lookup-based Fargate service stack
- `bro/oops/cdk/app.py` — testable assembly for the repository, image-build, and trails stacks
- `deployment/app.py` — this repository's CDK entry point;
  its `cdk.json` runs it through `uv run`, which resolves this project and so synthesizes in `.venv`
