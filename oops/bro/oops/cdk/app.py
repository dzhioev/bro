from dataclasses import dataclass
from typing import Optional

import aws_cdk as cdk

from bro.oops.cdk.config import InfrastructureConfig
from bro.oops.cdk.ecr import RepositoryStack
from bro.oops.cdk.image_build import ImageBuildStack
from bro.oops.cdk.trails import TrailsServerStack


@dataclass(frozen=True)
class DeploymentStacks:
  repository: RepositoryStack
  image_build: ImageBuildStack
  trails: TrailsServerStack


def create_app(
  infrastructure_config: InfrastructureConfig,
  account: str,
  *,
  app: Optional[cdk.App] = None,
  image_digest: Optional[str] = None,
) -> tuple[cdk.App, DeploymentStacks]:
  application = cdk.App() if app is None else app
  environment = cdk.Environment(account=account, region=infrastructure_config.region)
  repository_stack = RepositoryStack(
    application,
    infrastructure_config.trails_repository,
    env=environment,
  )
  image_build_stack = ImageBuildStack(application, infrastructure_config, env=environment)
  trails_stack = TrailsServerStack(
    application,
    infrastructure_config,
    image_digest=image_digest,
    env=environment,
  )
  trails_stack.add_dependency(repository_stack)
  return application, DeploymentStacks(
    repository_stack,
    image_build_stack,
    trails_stack,
  )
