from unittest.mock import patch

import pytest

from bro.oops.cdk.config import from_mapping, resolve


def test_defaults_are_consumer_neutral():
  config = from_mapping({'delegated_subdomain': 'services.example.com'})

  assert config.region == 'us-east-1'
  assert config.platform.stack_name == 'BroPlatformStack'
  assert config.platform.cluster_name == 'bro-services'
  assert config.repositories['trails'].repository_name == 'bro-trails-server'
  assert config.image_build.project_name == 'bro-image-build'
  assert config.image_build.connection_name is None
  assert config.image_build.connection_arn is None
  assert config.image_build.image_build_script == 'oops/image_build.sh'
  assert config.trails.spillover_bucket_name == 'bro-trails-{account}'
  assert config.repository_names == ('bro-trails-server',)


def test_a_shipped_repository_keeps_the_fields_its_entry_does_not_name():
  shipped = from_mapping({'delegated_subdomain': 'services.example.com'}).trails_repository

  config = from_mapping(
    {
      'delegated_subdomain': 'services.example.com',
      'oops': {'repositories': {'trails': {'stack_name': 'LegacyTrailsECRStack'}}},
    }
  )

  assert config.trails_repository.stack_name == 'LegacyTrailsECRStack'
  assert config.trails_repository.repository_name == shipped.repository_name
  assert config.trails_repository.repository_construct_id == shipped.repository_construct_id


def test_account_names_are_resolved_from_the_infra_namespace():
  raw = {
    'delegated_subdomain': 'apps.example.net',
    'unrelated_consumer_field': 'preserved',
    'oops': {
      'region': 'eu-west-1',
      'platform': {
        'stack_name': 'CustomPlatform',
        'cluster_name': 'custom-services',
      },
      'repositories': {
        'api': {
          'stack_name': 'CustomAPIRepository',
          'repository_name': 'custom-api',
          'repository_construct_id': 'APIRepository',
        },
        'worker': {
          'stack_name': 'CustomWorkerRepository',
          'repository_name': 'custom-worker',
          'repository_construct_id': 'WorkerRepository',
        },
      },
      'image_build': {
        'stack_name': 'CustomImageBuild',
        'project_name': 'custom-image-build',
        'connection_name': 'custom-github',
        'source_owner': 'organization',
        'source_repository': 'application',
        'buildspec_path': 'deployment/buildspec.yml',
        'image_build_script': 'deployment/image_build.sh',
      },
      'trails': {
        'stack_name': 'CustomTrails',
        'repository': 'api',
        'spillover_bucket_name': 'custom-trails-{account}',
        'service_name': 'custom-trails',
      },
    },
  }

  config = from_mapping(raw)

  assert config.region == 'eu-west-1'
  assert config.delegated_subdomain == 'apps.example.net'
  assert config.platform.stack_name == 'CustomPlatform'
  assert config.platform.cluster_name == 'custom-services'
  assert config.repositories['api'].repository_construct_id == 'APIRepository'
  assert config.repository_names == ('custom-api', 'custom-worker')
  assert config.image_build.source_owner == 'organization'
  assert config.image_build.buildspec_path == 'deployment/buildspec.yml'
  assert config.image_build.image_build_script == 'deployment/image_build.sh'
  assert config.trails.stack_name == 'CustomTrails'
  assert config.trails_repository.repository_name == 'custom-api'


def test_resolve_reads_the_infra_credential():
  raw = {'delegated_subdomain': 'services.example.com'}
  with patch('bro.oops.cdk.config.credentials.get_json', return_value=raw) as get_json:
    config = resolve()

  assert config.delegated_subdomain == 'services.example.com'
  get_json.assert_called_once_with('infra')


@pytest.mark.parametrize(
  ('raw', 'message'),
  [
    ({}, 'infra.delegated_subdomain is required'),
    (
      {'delegated_subdomain': 'services.example.com', 'oops': {'platfrom': {}}},
      'infra.oops has unknown fields: platfrom',
    ),
    (
      {'delegated_subdomain': 'services.example.com', 'oops': {'repositories': {}}},
      'infra.oops.repositories must contain at least one repository',
    ),
    (
      {'delegated_subdomain': 'services.example.com', 'oops': {'region': 1}},
      'infra.oops.region must be a non-empty string',
    ),
    (
      {
        'delegated_subdomain': 'services.example.com',
        'oops': {
          'repositories': {
            'api': {
              'stack_name': 'APIRepository',
              'repository_name': 'api',
              'repository_construct_id': 'APIRepository',
            }
          }
        },
      },
      "infra.oops.trails.repository names unknown repository 'trails'",
    ),
    (
      {
        'delegated_subdomain': 'services.example.com',
        'oops': {'repositories': {'api': {'stack_name': 'APIRepository'}}},
      },
      'infra.oops.repositories.api.repository_name is required',
    ),
    (
      {
        'delegated_subdomain': 'services.example.com',
        'oops': {'trails': {'spillover_bucket_name': 'trails-{region}'}},
      },
      'spillover_bucket_name supports only',
    ),
    (
      {
        'delegated_subdomain': 'services.example.com',
        'oops': {
          'image_build': {
            'connection_name': 'custom-github',
            'connection_arn': 'arn:aws:codeconnections:eu-central-1:1:connection/abc',
          }
        },
      },
      'sets both connection_name and connection_arn',
    ),
  ],
)
def test_invalid_config_fails_fast(raw, message):
  with pytest.raises(ValueError, match=message):
    from_mapping(raw)
