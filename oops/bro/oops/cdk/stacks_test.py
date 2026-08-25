import aws_cdk as cdk
from aws_cdk import assertions

from bro.oops.cdk import (
  HostedZoneReference,
  ImageBuildStack,
  PlatformStack,
  RepositoryStack,
  from_mapping,
)

_ACCOUNT = '111111111111'
_DOMAIN = 'services.example.com'
_ZONE_ID = 'Z0123456789'


def _stacks(tmp_path):
  config = from_mapping({'delegated_subdomain': _DOMAIN})
  app = cdk.App(outdir=str(tmp_path / 'cdk.out'))
  environment = cdk.Environment(account=_ACCOUNT, region=config.region)
  platform = PlatformStack(
    app,
    config,
    hosted_zone=HostedZoneReference(hosted_zone_id=_ZONE_ID, zone_name=_DOMAIN),
    env=environment,
  )
  repository = RepositoryStack(app, config.repositories['server'], env=environment)
  image_build = ImageBuildStack(app, config, env=environment)
  return app, platform, repository, image_build


def test_platform_stack_preserves_its_construct_ids(tmp_path):
  _, stack, _, _ = _stacks(tmp_path)
  template = assertions.Template.from_stack(stack)

  assert stack.stack_name == 'BroPlatformStack'
  assert set(template.to_json()['Resources']) == {
    'PlatformVpc423616FE',
    'PlatformVpcPublicSubnet1SubnetC0921B5D',
    'PlatformVpcPublicSubnet1RouteTable33B25D83',
    'PlatformVpcPublicSubnet1RouteTableAssociationDED98CD0',
    'PlatformVpcPublicSubnet1DefaultRouteD965B32B',
    'PlatformVpcPublicSubnet2Subnet0CB3B183',
    'PlatformVpcPublicSubnet2RouteTableDBBBB5C7',
    'PlatformVpcPublicSubnet2RouteTableAssociation756EF210',
    'PlatformVpcPublicSubnet2DefaultRouteA630D8A1',
    'PlatformVpcIGW237ABAED',
    'PlatformVpcVPCGW7B6F9C77',
    'PlatformClusterF99F789E',
    'WildcardCert4A8FDF87',
    'PlatformAlb03CABEBB',
    'PlatformAlbSecurityGroupDE8F554F',
    'PlatformAlbHttpsListener21F81026',
    'PlatformAlbHttpRedirectF29B7315',
  }
  template.has_resource_properties('AWS::ECS::Cluster', {'ClusterName': 'bro-services'})
  template.has_resource_properties(
    'AWS::CertificateManager::Certificate',
    {'DomainName': f'*.{_DOMAIN}', 'DomainValidationOptions': [{'DomainName': f'*.{_DOMAIN}'}]},
  )
  template.resource_count_is('AWS::ElasticLoadBalancingV2::Listener', 2)
  assert stack.handles.cluster is stack.cluster
  assert stack.handles.https_listener is stack.https_listener


def test_repository_stack_preserves_repository_and_output_ids(tmp_path):
  _, _, stack, _ = _stacks(tmp_path)
  template = assertions.Template.from_stack(stack)

  assert stack.stack_name == 'BroServerECRStack'
  assert set(template.to_json()['Resources']) == {'ServerRepo9563DA5D'}
  assert set(template.to_json()['Outputs']) == {'ECRRepoURI'}
  template.has_resource_properties('AWS::ECR::Repository', {'RepositoryName': 'bro-server'})


def test_image_build_stack_is_parameterized_and_preserves_construct_ids(tmp_path):
  _, _, _, stack = _stacks(tmp_path)
  template = assertions.Template.from_stack(stack)

  assert stack.stack_name == 'BroImageBuildStack'
  assert set(template.to_json()['Resources']) == {
    'GitHubConnection',
    'GitHubSourceCredential',
    'ImageBuildRoleAFF19194',
    'ImageBuildRoleDefaultPolicyBD902299',
    'ImageBuild30B7C98D',
  }
  template.has_resource_properties(
    'AWS::CodeConnections::Connection',
    {'ConnectionName': 'bro-github', 'ProviderType': 'GitHub'},
  )
  template.has_resource_properties(
    'AWS::CodeBuild::Project',
    {
      'Name': 'bro-image-build',
      'Source': {
        'BuildSpec': 'infra/buildspec.yml',
        'Location': 'https://github.com/example/deployment.git',
        'ReportBuildStatus': True,
        'Type': 'GITHUB',
      },
    },
  )


def test_all_stacks_synthesize_without_aws_access(tmp_path):
  app, _, _, _ = _stacks(tmp_path)

  assembly = app.synth()

  assert {artifact.stack_name for artifact in assembly.stacks} == {
    'BroImageBuildStack',
    'BroPlatformStack',
    'BroServerECRStack',
  }
