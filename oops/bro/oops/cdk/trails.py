# pyright: reportArgumentType=false
from typing import Optional

import boto3
from aws_cdk import (
  CfnOutput,
  Duration,
  RemovalPolicy,
  Stack,
  aws_dynamodb as dynamodb,
  aws_ec2 as ec2,
  aws_ecr as ecr,
  aws_ecs as ecs,
  aws_elasticloadbalancingv2 as elbv2,
  aws_iam as iam,
  aws_logs as logs,
  aws_route53 as route53,
  aws_route53_targets as targets,
  aws_s3 as s3,
  aws_ssm as ssm,
)
from constructs import Construct

from bro.oops.cdk.config import InfrastructureConfig
from bro.oops.cdk.platform import PlatformHandles
from bro.trails.server import dynamo as trails_dynamo

CONTAINER_PORT = 8004
STORE_CONFIG_PARAMETER = '/trails/store-config'
TOKENS_PARAMETER = '/trails/tokens'
PLACEHOLDER_DIGEST = 'sha256:' + '0' * 64

_ATTRIBUTE_TYPES = {
  'string': dynamodb.AttributeType.STRING,
  'number': dynamodb.AttributeType.NUMBER,
}
_PROJECTION_TYPES = {
  'all': dynamodb.ProjectionType.ALL,
  'keys_only': dynamodb.ProjectionType.KEYS_ONLY,
}


def get_image_digest(repository_name: str, region: str) -> str:
  client = boto3.client('ecr', region_name=region)
  try:
    response = client.describe_images(
      repositoryName=repository_name,
      imageIds=[{'imageTag': 'latest'}],
    )
    return response['imageDetails'][0]['imageDigest']
  except (client.exceptions.ImageNotFoundException, client.exceptions.RepositoryNotFoundException):
    return PLACEHOLDER_DIGEST


def _attribute(attribute: trails_dynamo.DynamoAttribute) -> dynamodb.Attribute:
  return dynamodb.Attribute(name=attribute.name, type=_ATTRIBUTE_TYPES[attribute.type])


def _table(
  stack: Stack,
  construct_id: str,
  schema: trails_dynamo.DynamoTable,
) -> dynamodb.Table:
  table = dynamodb.Table(
    stack,
    construct_id,
    table_name=schema.name,
    partition_key=_attribute(schema.partition_key),
    sort_key=_attribute(schema.sort_key) if schema.sort_key is not None else None,
    billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
    removal_policy=RemovalPolicy.RETAIN,
  )
  for index in schema.indexes:
    table.add_global_secondary_index(
      index_name=index.name,
      partition_key=_attribute(index.partition_key),
      sort_key=_attribute(index.sort_key) if index.sort_key is not None else None,
      projection_type=_PROJECTION_TYPES[index.projection],
    )
  return table


class TrailsServerStack(Stack):
  def __init__(
    self,
    scope: Construct,
    infrastructure_config: InfrastructureConfig,
    *,
    platform: Optional[PlatformHandles] = None,
    image_digest: Optional[str] = None,
    **kwargs,
  ) -> None:
    config = infrastructure_config.trails
    repository_config = infrastructure_config.trails_repository
    super().__init__(scope, config.stack_name, **kwargs)

    if platform is None:
      platform = PlatformHandles.lookup(
        self,
        infrastructure_config.platform,
        infrastructure_config.delegated_subdomain,
      )

    repository = ecr.Repository.from_repository_name(
      self,
      'TrailsServerRepo',
      repository_config.repository_name,
    )
    digest = (
      get_image_digest(repository_config.repository_name, infrastructure_config.region)
      if image_digest is None
      else image_digest
    )

    trails_table = _table(
      self,
      'UniversalTrailsTable',
      trails_dynamo.TRAILS_TABLE,
    )
    steps_table = _table(
      self,
      'UniversalTrailStepsTable',
      trails_dynamo.STEPS_TABLE,
    )

    spillover_bucket = s3.Bucket(
      self,
      'TrailsSpilloverBucket',
      bucket_name=config.spillover_bucket_name.replace('{account}', self.account),
      removal_policy=RemovalPolicy.RETAIN,
    )

    store_config = ssm.StringParameter(
      self,
      'TrailsStoreConfig',
      parameter_name=STORE_CONFIG_PARAMETER,
      string_value=self.to_json_string(
        {
          'backend': 'dynamo',
          'trails_table': trails_dynamo.TRAILS_TABLE.name,
          'steps_table': trails_dynamo.STEPS_TABLE.name,
          'uuid_index': trails_dynamo.UUID_INDEX,
          'bucket': spillover_bucket.bucket_name,
          'region': infrastructure_config.region,
        }
      ),
    )

    execution_role = iam.Role(
      self,
      'TaskExecutionRole',
      assumed_by=iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      managed_policies=[
        iam.ManagedPolicy.from_aws_managed_policy_name(
          'service-role/AmazonECSTaskExecutionRolePolicy'
        ),
      ],
    )

    task_role = iam.Role(
      self,
      'TaskRole',
      assumed_by=iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
    )
    trails_table.grant_read_write_data(task_role)
    steps_table.grant_read_write_data(task_role)
    spillover_bucket.grant_read_write(task_role)
    store_config.grant_read(task_role)
    ssm.StringParameter.from_secure_string_parameter_attributes(
      self,
      'TrailsTokens',
      parameter_name=TOKENS_PARAMETER,
    ).grant_read(task_role)
    task_role.add_to_policy(
      iam.PolicyStatement(
        actions=['kms:Decrypt'],
        resources=[f'arn:aws:kms:{self.region}:{self.account}:alias/aws/ssm'],
      )
    )

    task_definition = ecs.FargateTaskDefinition(
      self,
      'TrailsTaskDef',
      cpu=256,
      memory_limit_mib=512,
      execution_role=execution_role,
      task_role=task_role,
    )

    task_definition.add_container(
      'trails-server',
      image=ecs.ContainerImage.from_ecr_repository(repository, digest),
      port_mappings=[ecs.PortMapping(container_port=CONTAINER_PORT)],
      environment={
        'HOST': '0.0.0.0',
        'PORT': str(CONTAINER_PORT),
        'IMAGE_DIGEST': digest,
      },
      logging=ecs.LogDriver.aws_logs(
        stream_prefix='trails',
        log_retention=logs.RetentionDays.TWO_WEEKS,
      ),
    )

    service = ecs.FargateService(
      self,
      'TrailsService',
      cluster=platform.cluster,
      task_definition=task_definition,
      # Two tasks keep mandatory recording available through one task or AZ loss.
      desired_count=2,
      assign_public_ip=True,
      vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
      service_name=config.service_name,
      circuit_breaker=ecs.DeploymentCircuitBreaker(enable=True, rollback=True),
    )
    service.node.add_dependency(store_config)

    target_group = elbv2.ApplicationTargetGroup(
      self,
      'TrailsTargetGroup',
      port=CONTAINER_PORT,
      protocol=elbv2.ApplicationProtocol.HTTP,
      targets=[service],
      vpc=platform.vpc,
      health_check=elbv2.HealthCheck(
        path='/health',
        healthy_http_codes='200',
        interval=Duration.seconds(30),
      ),
    )

    elbv2.ApplicationListenerRule(
      self,
      'TrailsListenerRule',
      listener=platform.https_listener,
      priority=30,
      conditions=[
        elbv2.ListenerCondition.host_headers(
          [f'trails.{infrastructure_config.delegated_subdomain}']
        ),
      ],
      target_groups=[target_group],
    )

    ingress_rules = [
      construct
      for construct in self.node.find_all()
      if isinstance(construct, ec2.CfnSecurityGroupIngress)
    ]
    egress_rules = [
      construct
      for construct in self.node.find_all()
      if isinstance(construct, ec2.CfnSecurityGroupEgress)
    ]
    if len(ingress_rules) != 1 or len(egress_rules) != 1:
      raise ValueError(
        'trails listener must create exactly one ingress and one egress security-group rule'
      )
    ingress_rules[0].override_logical_id(config.load_balancer_ingress_logical_id)
    egress_rules[0].override_logical_id(config.load_balancer_egress_logical_id)
    # a same-app platform orders the egress rule after these resources, while a
    # lookup-imported security group generates no such edges; pinning them keeps
    # both assemblies' templates identical
    egress_rules[0].node.add_dependency(
      task_role,
      task_role.node.find_child('DefaultPolicy'),
      store_config,
    )

    route53.ARecord(
      self,
      'TrailsDNSRecord',
      zone=platform.hosted_zone,
      record_name='trails',
      target=route53.RecordTarget.from_alias(targets.LoadBalancerTarget(platform.load_balancer)),
    )

    CfnOutput(
      self,
      'ServiceURL',
      value=f'https://trails.{infrastructure_config.delegated_subdomain}',
    )
