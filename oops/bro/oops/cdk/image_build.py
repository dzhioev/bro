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

    connection_arn = config.connection_arn
    if config.connection_name is not None:
      connection = codeconnections.CfnConnection(
        self,
        'GitHubConnection',
        connection_name=config.connection_name,
        provider_type='GitHub',
      )
      connection_arn = connection.attr_connection_arn
      CfnOutput(self, 'ConnectionARN', value=connection_arn)

    self.project = codebuild.Project(
      self,
      'ImageBuild',
      project_name=config.project_name,
      source=codebuild.Source.git_hub(
        owner=config.source_owner,
        repo=config.source_repository,
        webhook=False,
        report_build_status=connection_arn is not None,
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
    if connection_arn is not None:
      project_resource = self.project.node.default_child
      assert isinstance(project_resource, codebuild.CfnProject)
      # the L2 GitHub source exposes no auth property
      project_resource.add_property_override(
        'Source.Auth', {'Type': 'CODECONNECTIONS', 'Resource': connection_arn}
      )
      connection_id = Fn.select(1, Fn.split(':connection/', connection_arn))
      role = self.project.role
      assert role is not None
      # CodeBuild rejects a project whose service role cannot already reach the
      # connection, so the grant is a policy of its own that the project waits
      # on; the role's default policy cannot serve, since it names the project
      # and depending on it would close a cycle
      connection_access = iam.Policy(
        self,
        'ConnectionAccess',
        statements=[
          iam.PolicyStatement(
            actions=[
              'codeconnections:GetConnection',
              'codeconnections:GetConnectionToken',
              'codestar-connections:GetConnection',
              'codestar-connections:GetConnectionToken',
              'codestar-connections:UseConnection',
            ],
            resources=[
              connection_arn,
              self.format_arn(
                service='codestar-connections',
                resource='connection',
                resource_name=connection_id,
                arn_format=ArnFormat.SLASH_RESOURCE_NAME,
              ),
            ],
          )
        ],
      )
      connection_access.attach_to_role(role)
      connection_access_resource = connection_access.node.default_child
      assert isinstance(connection_access_resource, iam.CfnPolicy)
      project_resource.add_dependency(connection_access_resource)

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
