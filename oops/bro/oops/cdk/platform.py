from dataclasses import dataclass
from typing import Optional

import jsii
from aws_cdk import (
  Aspects,
  Duration,
  IAspect,
  Stack,
  aws_certificatemanager as acm,
  aws_ec2 as ec2,
  aws_ecs as ecs,
  aws_elasticloadbalancingv2 as elbv2,
  aws_route53 as route53,
)
from constructs import Construct, IConstruct

from bro.oops.cdk.config import InfrastructureConfig, PlatformConfig

_LOAD_BALANCER_DISALLOW_ALL_EGRESS = [
  {
    'cidrIp': '255.255.255.255/32',
    'description': 'Disallow all traffic',
    'fromPort': 252,
    'ipProtocol': 'icmp',
    'toPort': 86,
  }
]


@jsii.implements(IAspect)
class _LoadBalancerSecurityGroupRulesAspect:
  def __init__(self, security_group: ec2.CfnSecurityGroup) -> None:
    self._security_group_node_address = security_group.node.addr

  def visit(self, node: IConstruct) -> None:
    if node.node.addr != self._security_group_node_address:
      return
    if not isinstance(node, ec2.CfnSecurityGroup):
      raise TypeError('platform load balancer security group must synthesize a CfnSecurityGroup')

    inline_egress = Stack.of(node).resolve(node.security_group_egress)
    if inline_egress != _LOAD_BALANCER_DISALLOW_ALL_EGRESS:
      raise ValueError(
        "platform ALB security group may carry only CDK's disallow-all inline egress "
        'placeholder; grant ALB-to-service permissions from each service stack as '
        'standalone security group rules, never inline on the platform security group'
      )


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
    listener_lookup = elbv2.ApplicationListener.from_lookup(
      scope,
      'PlatformHTTPSListenerLookup',
      listener_port=443,
      load_balancer_tags=stack_tags,
    )
    security_groups = load_balancer.connections.security_groups
    if len(security_groups) != 1:
      raise ValueError(
        f'platform load balancer must have one security group, found {len(security_groups)}'
      )
    listener_security_group = ec2.SecurityGroup.from_security_group_id(
      scope,
      'PlatformLoadBalancerSecurityGroup',
      security_groups[0].security_group_id,
      allow_all_outbound=False,
    )
    https_listener = elbv2.ApplicationListener.from_application_listener_attributes(
      scope,
      'PlatformHTTPSListener',
      listener_arn=listener_lookup.listener_arn,
      security_group=listener_security_group,
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
      'PlatformVpc',
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
      'PlatformCluster',
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
      'PlatformAlb',
      vpc=self.vpc,
      internet_facing=True,
      idle_timeout=Duration.seconds(300),
    )
    security_groups = self.load_balancer.connections.security_groups
    if len(security_groups) != 1:
      raise ValueError(
        f'platform load balancer must have one security group, found {len(security_groups)}'
      )
    security_group_resource = security_groups[0].node.default_child
    if not isinstance(security_group_resource, ec2.CfnSecurityGroup):
      raise TypeError('platform load balancer security group must synthesize a CfnSecurityGroup')
    Aspects.of(self).add(_LoadBalancerSecurityGroupRulesAspect(security_group_resource))

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
