import json

import aws_cdk as cdk
from aws_cdk import assertions

from bro.oops.cdk import (
  HostedZoneReference,
  ImageBuildStack,
  PlatformStack,
  RepositoryStack,
  TrailsServerStack,
  create_app,
  from_mapping,
)

_ACCOUNT = '111111111111'
_DOMAIN = 'services.example.com'
_ZONE_ID = 'Z0123456789'
_DIGEST = 'sha256:' + '1' * 64


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
  repository = RepositoryStack(app, config.repositories['trails'], env=environment)
  image_build = ImageBuildStack(app, config, env=environment)
  return app, platform, repository, image_build


def test_platform_stack_creates_the_shared_platform(tmp_path):
  _, stack, _, _ = _stacks(tmp_path)
  template = assertions.Template.from_stack(stack)

  assert stack.stack_name == 'BroPlatformStack'
  # pins the logical ids of a deployed platform: renaming a construct here
  # replaces the live VPC, cluster or load balancer on the next deploy
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

  assert stack.stack_name == 'BroTrailsECRStack'
  assert set(template.to_json()['Resources']) == {'TrailsServerRepo8F441D85'}
  assert set(template.to_json()['Outputs']) == {'ECRRepoURI'}
  template.has_resource_properties('AWS::ECR::Repository', {'RepositoryName': 'bro-trails-server'})


def _image_build_template(tmp_path, overrides):
  config = from_mapping({'delegated_subdomain': _DOMAIN, 'oops': {'image_build': overrides}})
  app = cdk.App(outdir=str(tmp_path / 'cdk.out'))
  stack = ImageBuildStack(
    app,
    config,
    env=cdk.Environment(account=_ACCOUNT, region=config.region),
  )
  return stack, assertions.Template.from_stack(stack)


def test_image_build_stack_is_parameterized_and_preserves_construct_ids(tmp_path):
  _, _, _, stack = _stacks(tmp_path)
  template = assertions.Template.from_stack(stack)

  assert stack.stack_name == 'BroImageBuildStack'
  assert set(template.to_json()['Resources']) == {
    'ImageBuildRoleAFF19194',
    'ImageBuildRoleDefaultPolicyBD902299',
    'ImageBuild30B7C98D',
  }
  template.has_resource_properties(
    'AWS::CodeBuild::Project',
    {
      'Name': 'bro-image-build',
      'Source': {
        'BuildSpec': 'oops/bro/oops/infra/buildspec.yml',
        'Location': 'https://github.com/example/deployment.git',
        'ReportBuildStatus': False,
        'Type': 'GITHUB',
      },
      'Environment': {
        'EnvironmentVariables': [
          {
            'Name': 'IMAGE_BUILD_SCRIPT',
            'Type': 'PLAINTEXT',
            'Value': 'oops/image_build.sh',
          }
        ]
      },
    },
  )


def test_image_build_stack_without_an_identity_grants_no_connection_access(tmp_path):
  _, template = _image_build_template(tmp_path, {})

  assert 'Auth' not in template.to_json()['Resources']['ImageBuild30B7C98D']['Properties']['Source']
  statements = template.to_json()['Resources']['ImageBuildRoleDefaultPolicyBD902299']['Properties'][
    'PolicyDocument'
  ]['Statement']
  assert all('codeconnections:GetConnection' not in s['Action'] for s in statements)


def test_image_build_stack_creates_the_named_connection_and_builds_as_it(tmp_path):
  _, template = _image_build_template(tmp_path, {'connection_name': 'bro-github'})

  resources = template.to_json()['Resources']
  assert 'GitHubSourceCredential' not in resources
  template.has_resource_properties(
    'AWS::CodeConnections::Connection',
    {'ConnectionName': 'bro-github', 'ProviderType': 'GitHub'},
  )
  source = resources['ImageBuild30B7C98D']['Properties']['Source']
  assert source['ReportBuildStatus'] is True
  assert source['Auth'] == {
    'Type': 'CODECONNECTIONS',
    'Resource': {'Fn::GetAtt': ['GitHubConnection', 'ConnectionArn']},
  }


def test_image_build_stack_builds_as_an_existing_connection(tmp_path):
  arn = 'arn:aws:codeconnections:us-east-1:111111111111:connection/abc'
  _, template = _image_build_template(tmp_path, {'connection_arn': arn})

  resources = template.to_json()['Resources']
  assert 'GitHubConnection' not in resources
  assert resources['ImageBuild30B7C98D']['Properties']['Source']['Auth'] == {
    'Type': 'CODECONNECTIONS',
    'Resource': arn,
  }
  access = resources['ConnectionAccessE5344D3B']
  assert arn in str(access['Properties']['PolicyDocument']['Statement'])
  assert resources['ImageBuild30B7C98D']['DependsOn'] == ['ConnectionAccessE5344D3B']


def test_trails_stack_preserves_resources_and_schema(tmp_path):
  config = from_mapping({'delegated_subdomain': _DOMAIN})
  app = cdk.App(outdir=str(tmp_path / 'cdk.out'))
  stack = TrailsServerStack(
    app,
    config,
    image_digest=_DIGEST,
    env=cdk.Environment(account=_ACCOUNT, region=config.region),
  )
  template = assertions.Template.from_stack(stack)

  assert stack.stack_name == 'BroTrailsServerStack'
  assert set(template.to_json()['Resources']) == {
    'PlatformLoadBalancerSecurityGrouptoBroTrailsServerStackTrailsServiceSecurityGroup1A4A1C358004D5F2728C',
    'TaskExecutionRole250D2532',
    'TaskExecutionRoleDefaultPolicyA84DD1B0',
    'TaskRole30FC0FBB',
    'TaskRoleDefaultPolicy07FC53DE',
    'TrailsDNSRecordDF0E7F5D',
    'TrailsListenerRule6DB69EDC',
    'TrailsService6D10D106',
    'TrailsServiceSecurityGroup58DDDDEE',
    'TrailsServiceSecurityGroupfromBroTrailsServerStackPlatformLoadBalancerSecurityGroupC26F5B8680041A4A149B',
    'TrailsSpilloverBucket3928E3BC',
    'TrailsStoreConfigE193DC52',
    'TrailsTargetGroup3CD82A7D',
    'TrailsTaskDef58071EE2',
    'TrailsTaskDeftrailsserverLogGroupCDFF4313',
    'UniversalTrailStepsTableCB25A5EA',
    'UniversalTrailsTableF6632AF7',
  }
  template.has_resource_properties(
    'AWS::DynamoDB::Table',
    {
      'TableName': 'trails-v2',
      'KeySchema': [{'AttributeName': 'id', 'KeyType': 'HASH'}],
    },
  )
  template.has_resource_properties(
    'AWS::DynamoDB::Table',
    {
      'TableName': 'trail_steps_v2',
      'KeySchema': [
        {'AttributeName': 'trail_id', 'KeyType': 'HASH'},
        {'AttributeName': 'step_id', 'KeyType': 'RANGE'},
      ],
    },
  )
  template.has_resource_properties('AWS::S3::Bucket', {'BucketName': f'bro-trails-{_ACCOUNT}'})
  template.has_resource_properties('AWS::SSM::Parameter', {'Name': '/trails/store-config'})
  template.has_resource_properties(
    'AWS::ECS::Service', {'ServiceName': 'trails-server', 'DesiredCount': 2}
  )
  template.has_resource_properties('AWS::ElasticLoadBalancingV2::ListenerRule', {'Priority': 30})
  template.has_resource_properties('AWS::Route53::RecordSet', {'Name': f'trails.{_DOMAIN}.'})
  assert set(template.to_json()['Outputs']) == {'ServiceURL'}


def test_repository_app_carries_only_its_own_stacks(tmp_path):
  config = from_mapping({'delegated_subdomain': _DOMAIN})
  app = cdk.App(outdir=str(tmp_path / 'cdk.out'))

  application, stacks = create_app(config, _ACCOUNT, app=app, image_digest=_DIGEST)

  assembly = application.synth()

  assert {artifact.stack_name for artifact in assembly.stacks} == {
    'BroTrailsECRStack',
    'BroImageBuildStack',
    'BroTrailsServerStack',
  }
  assert set(stacks.trails.dependencies) == {stacks.repository}
  trails = json.dumps(assertions.Template.from_stack(stacks.trails).to_json())
  assert 'Fn::ImportValue' not in trails
