from aws_cdk import (
  ArnFormat,
  CfnOutput,
  Duration,
  Fn,
  Stack,
  aws_codebuild as codebuild,
  aws_codeconnections as codeconnections,
  aws_iam as iam,
)
from constructs import Construct

from bro.oops.cdk.config import InfrastructureConfig


class ImageBuildStack(Stack):
  def __init__(
    self, scope: Construct, infrastructure_config: InfrastructureConfig, **kwargs
  ) -> None:
    config = infrastructure_config.image_build
    super().__init__(scope, config.stack_name, **kwargs)

    connection = codeconnections.CfnConnection(
      self,
      'GitHubConnection',
      connection_name=config.connection_name,
      provider_type='GitHub',
    )
    source_credential = codebuild.CfnSourceCredential(
      self,
      'GitHubSourceCredential',
      auth_type='CODECONNECTIONS',
      server_type='GITHUB',
      token=connection.attr_connection_arn,
    )

    self.project = codebuild.Project(
      self,
      'ImageBuild',
      project_name=config.project_name,
      source=codebuild.Source.git_hub(
        owner=config.source_owner,
        repo=config.source_repository,
        webhook=False,
      ),
      build_spec=codebuild.BuildSpec.from_source_filename(config.buildspec_path),
      environment=codebuild.BuildEnvironment(
        build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
        compute_type=codebuild.ComputeType.MEDIUM,
        privileged=True,
      ),
      environment_variables={
        'IMAGE_BUILD_SCRIPT': codebuild.BuildEnvironmentVariable(value=config.image_build_script)
      },
      cache=codebuild.Cache.local(
        codebuild.LocalCacheMode.SOURCE,
        codebuild.LocalCacheMode.DOCKER_LAYER,
      ),
      timeout=Duration.minutes(30),
    )
    project_resource = self.project.node.default_child
    assert isinstance(project_resource, codebuild.CfnProject)
    project_resource.add_dependency(source_credential)

    connection_id = Fn.select(1, Fn.split(':connection/', connection.attr_connection_arn))
    self.project.add_to_role_policy(
      iam.PolicyStatement(
        actions=[
          'codeconnections:GetConnection',
          'codeconnections:GetConnectionToken',
          'codestar-connections:GetConnection',
          'codestar-connections:GetConnectionToken',
          'codestar-connections:UseConnection',
        ],
        resources=[
          connection.attr_connection_arn,
          self.format_arn(
            service='codestar-connections',
            resource='connection',
            resource_name=connection_id,
            arn_format=ArnFormat.SLASH_RESOURCE_NAME,
          ),
        ],
      )
    )
    self.project.add_to_role_policy(
      iam.PolicyStatement(actions=['ecr:GetAuthorizationToken'], resources=['*'])
    )
    self.project.add_to_role_policy(
      iam.PolicyStatement(
        actions=[
          'ecr:BatchCheckLayerAvailability',
          'ecr:BatchGetImage',
          'ecr:CompleteLayerUpload',
          'ecr:GetDownloadUrlForLayer',
          'ecr:InitiateLayerUpload',
          'ecr:PutImage',
          'ecr:UploadLayerPart',
        ],
        resources=[
          self.format_arn(service='ecr', resource='repository', resource_name=name)
          for name in infrastructure_config.repository_names
        ],
      )
    )

    CfnOutput(self, 'ConnectionARN', value=connection.attr_connection_arn)
