from aws_cdk import CfnOutput, Stack, aws_ecr as ecr
from constructs import Construct

from bro.oops.cdk.config import RepositoryConfig


class RepositoryStack(Stack):
  def __init__(self, scope: Construct, config: RepositoryConfig, **kwargs) -> None:
    super().__init__(scope, config.stack_name, **kwargs)

    self.repository = ecr.Repository(
      self,
      config.repository_construct_id,
      repository_name=config.repository_name,
      lifecycle_rules=[ecr.LifecycleRule(max_image_count=10)],
    )

    CfnOutput(self, 'ECRRepoURI', value=self.repository.repository_uri)
