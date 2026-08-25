from dataclasses import dataclass
from typing import Optional

from aws_cdk import (
  Duration,
  Stack,
  aws_certificatemanager as acm,
  aws_ec2 as ec2,
  aws_ecs as ecs,
  aws_elasticloadbalancingv2 as elbv2,
  aws_route53 as route53,
)
from constructs import Construct

from bro.oops.cdk.config import InfrastructureConfig, PlatformConfig


@dataclass(frozen=True)
class HostedZoneReference:
  hosted_zone_id: str
  zone_name: str


@dataclass(frozen=True)
class PlatformHandles:
  vpc: ec2.IVpc
  cluster: ecs.ICluster
  hosted_zone: route53.IHostedZone
  load_balancer: elbv2.IApplicationLoadBalancer
  https_listener: elbv2.IApplicationListener

  @classmethod
  def lookup(
    cls,
    scope: Construct,
    config: PlatformConfig,
    delegated_subdomain: str,
  ) -> 'PlatformHandles':
    stack_tags = {'aws:cloudformation:stack-name': config.stack_name}
    vpc = ec2.Vpc.from_lookup(scope, 'PlatformVpc', tags=stack_tags)
    cluster = ecs.Cluster.from_cluster_attributes(
      scope,
      'PlatformCluster',
      cluster_name=config.cluster_name,
      vpc=vpc,
    )
    hosted_zone = route53.HostedZone.from_lookup(
      scope,
      'PlatformHostedZone',
      domain_name=delegated_subdomain,
    )
    load_balancer = elbv2.ApplicationLoadBalancer.from_lookup(
      scope,
      'PlatformLoadBalancer',
      load_balancer_tags=stack_tags,
    )
    https_listener = elbv2.ApplicationListener.from_lookup(
      scope,
      'PlatformHTTPSListener',
      listener_port=443,
      load_balancer_tags=stack_tags,
    )
    return cls(
      vpc=vpc,
      cluster=cluster,
      hosted_zone=hosted_zone,
      load_balancer=load_balancer,
      https_listener=https_listener,
    )


class PlatformStack(Stack):
  def __init__(
    self,
    scope: Construct,
    infrastructure_config: InfrastructureConfig,
    *,
    hosted_zone: Optional[HostedZoneReference] = None,
    **kwargs,
  ) -> None:
    config = infrastructure_config.platform
    super().__init__(scope, config.stack_name, **kwargs)

    self.vpc = ec2.Vpc(
      self,
      config.vpc_construct_id,
      max_azs=2,
      nat_gateways=0,
      subnet_configuration=[
        ec2.SubnetConfiguration(
          name='Public',
          subnet_type=ec2.SubnetType.PUBLIC,
          cidr_mask=24,
        ),
      ],
    )

    self.cluster = ecs.Cluster(
      self,
      config.cluster_construct_id,
      vpc=self.vpc,
      cluster_name=config.cluster_name,
    )

    self.hosted_zone = (
      route53.HostedZone.from_hosted_zone_attributes(
        self,
        'DelegatedZone',
        hosted_zone_id=hosted_zone.hosted_zone_id,
        zone_name=hosted_zone.zone_name,
      )
      if hosted_zone is not None
      else route53.HostedZone.from_lookup(
        self,
        'DelegatedZone',
        domain_name=infrastructure_config.delegated_subdomain,
      )
    )

    self.certificate = acm.Certificate(
      self,
      'WildcardCert',
      domain_name=f'*.{infrastructure_config.delegated_subdomain}',
      validation=acm.CertificateValidation.from_dns(self.hosted_zone),
    )

    self.load_balancer = elbv2.ApplicationLoadBalancer(
      self,
      config.load_balancer_construct_id,
      vpc=self.vpc,
      internet_facing=True,
      idle_timeout=Duration.seconds(300),
    )

    self.https_listener = self.load_balancer.add_listener(
      'HttpsListener',
      port=443,
      certificates=[self.certificate],
      default_action=elbv2.ListenerAction.fixed_response(
        status_code=404,
        content_type='text/plain',
        message_body='not found',
      ),
    )

    self.load_balancer.add_listener(
      'HttpRedirect',
      port=80,
      default_action=elbv2.ListenerAction.redirect(
        protocol='HTTPS',
        port='443',
        permanent=True,
      ),
    )

  @property
  def handles(self) -> PlatformHandles:
    return PlatformHandles(
      vpc=self.vpc,
      cluster=self.cluster,
      hosted_zone=self.hosted_zone,
      load_balancer=self.load_balancer,
      https_listener=self.https_listener,
    )
