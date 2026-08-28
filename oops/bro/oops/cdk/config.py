from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional

from bro.base import credentials

_DEFAULT_REGION = 'us-east-1'
_DEFAULT_PLATFORM = {
  'stack_name': 'BroPlatformStack',
  'cluster_name': 'bro-services',
  'vpc_construct_id': 'PlatformVpc',
  'cluster_construct_id': 'PlatformCluster',
  'load_balancer_construct_id': 'PlatformAlb',
}
_DEFAULT_REPOSITORIES = {
  'trails': {
    'stack_name': 'BroTrailsECRStack',
    'repository_name': 'bro-trails-server',
    'repository_construct_id': 'TrailsServerRepo',
  }
}
_DEFAULT_IMAGE_BUILD = {
  'stack_name': 'BroImageBuildStack',
  'project_name': 'bro-image-build',
  'connection_arn': None,
  'source_owner': 'example',
  'source_repository': 'deployment',
  'buildspec_path': 'oops/bro/oops/infra/buildspec.yml',
  'image_build_script': 'oops/image_build.sh',
}
_DEFAULT_TRAILS = {
  'stack_name': 'BroTrailsServerStack',
  'repository': 'trails',
  'spillover_bucket_name': 'bro-trails-{account}',
  'service_name': 'trails-server',
  'load_balancer_ingress_logical_id': 'TrailsLoadBalancerIngress',
  'load_balancer_egress_logical_id': 'TrailsLoadBalancerEgress',
}


@dataclass(frozen=True)
class PlatformConfig:
  stack_name: str
  cluster_name: str
  vpc_construct_id: str
  cluster_construct_id: str
  load_balancer_construct_id: str


@dataclass(frozen=True)
class RepositoryConfig:
  stack_name: str
  repository_name: str
  repository_construct_id: str


@dataclass(frozen=True)
class ImageBuildConfig:
  stack_name: str
  project_name: str
  connection_arn: Optional[str]
  source_owner: str
  source_repository: str
  buildspec_path: str
  image_build_script: str


@dataclass(frozen=True)
class TrailsConfig:
  stack_name: str
  repository: str
  spillover_bucket_name: str
  service_name: str
  load_balancer_ingress_logical_id: str
  load_balancer_egress_logical_id: str


@dataclass(frozen=True)
class InfrastructureConfig:
  region: str
  delegated_subdomain: str
  platform: PlatformConfig
  repositories: Mapping[str, RepositoryConfig]
  image_build: ImageBuildConfig
  trails: TrailsConfig

  @property
  def repository_names(self) -> tuple[str, ...]:
    return tuple(repository.repository_name for repository in self.repositories.values())

  @property
  def trails_repository(self) -> RepositoryConfig:
    try:
      return self.repositories[self.trails.repository]
    except KeyError as exception:
      raise ValueError(
        f'infra.oops.trails.repository names unknown repository {self.trails.repository!r}'
      ) from exception


def resolve() -> InfrastructureConfig:
  return from_mapping(credentials.get_json('infra'))


def from_mapping(raw: Mapping[str, Any]) -> InfrastructureConfig:
  delegated_subdomain = _required_string(raw, 'delegated_subdomain', 'infra')
  namespace = _optional_mapping(raw, 'oops', 'infra')
  _reject_unknown(
    namespace,
    {'region', 'platform', 'repositories', 'image_build', 'trails'},
    'infra.oops',
  )

  region = _optional_string(namespace, 'region', _DEFAULT_REGION, 'infra.oops')
  platform = _platform_config(_optional_mapping(namespace, 'platform', 'infra.oops'))
  repositories = _repository_configs(
    _mapping(namespace['repositories'], 'infra.oops.repositories')
    if 'repositories' in namespace
    else _DEFAULT_REPOSITORIES
  )
  image_build = _image_build_config(_optional_mapping(namespace, 'image_build', 'infra.oops'))
  trails = _trails_config(_optional_mapping(namespace, 'trails', 'infra.oops'))
  config = InfrastructureConfig(
    region=region,
    delegated_subdomain=delegated_subdomain,
    platform=platform,
    repositories=repositories,
    image_build=image_build,
    trails=trails,
  )
  _ = config.trails_repository
  return config


def _platform_config(overrides: Mapping[str, Any]) -> PlatformConfig:
  values = _with_defaults(_DEFAULT_PLATFORM, overrides, 'infra.oops.platform')
  return PlatformConfig(
    stack_name=_required_string(values, 'stack_name', 'infra.oops.platform'),
    cluster_name=_required_string(values, 'cluster_name', 'infra.oops.platform'),
    vpc_construct_id=_required_string(values, 'vpc_construct_id', 'infra.oops.platform'),
    cluster_construct_id=_required_string(values, 'cluster_construct_id', 'infra.oops.platform'),
    load_balancer_construct_id=_required_string(
      values, 'load_balancer_construct_id', 'infra.oops.platform'
    ),
  )


def _repository_configs(raw: Mapping[str, Any]) -> Mapping[str, RepositoryConfig]:
  repositories = {
    key: _repository_config(key, _mapping(value, f'infra.oops.repositories.{key}'))
    for key, value in raw.items()
  }
  if len(repositories) == 0:
    raise ValueError('infra.oops.repositories must contain at least one repository')
  return repositories


def _repository_config(key: str, raw: Mapping[str, Any]) -> RepositoryConfig:
  _reject_unknown(
    raw,
    {'stack_name', 'repository_name', 'repository_construct_id'},
    f'infra.oops.repositories.{key}',
  )
  return RepositoryConfig(
    stack_name=_required_string(raw, 'stack_name', f'infra.oops.repositories.{key}'),
    repository_name=_required_string(raw, 'repository_name', f'infra.oops.repositories.{key}'),
    repository_construct_id=_required_string(
      raw, 'repository_construct_id', f'infra.oops.repositories.{key}'
    ),
  )


def _image_build_config(overrides: Mapping[str, Any]) -> ImageBuildConfig:
  values = _with_defaults(_DEFAULT_IMAGE_BUILD, overrides, 'infra.oops.image_build')
  return ImageBuildConfig(
    stack_name=_required_string(values, 'stack_name', 'infra.oops.image_build'),
    project_name=_required_string(values, 'project_name', 'infra.oops.image_build'),
    connection_arn=_nullable_string(values, 'connection_arn', 'infra.oops.image_build'),
    source_owner=_required_string(values, 'source_owner', 'infra.oops.image_build'),
    source_repository=_required_string(values, 'source_repository', 'infra.oops.image_build'),
    buildspec_path=_required_string(values, 'buildspec_path', 'infra.oops.image_build'),
    image_build_script=_required_string(values, 'image_build_script', 'infra.oops.image_build'),
  )


def _trails_config(overrides: Mapping[str, Any]) -> TrailsConfig:
  values = _with_defaults(_DEFAULT_TRAILS, overrides, 'infra.oops.trails')
  bucket_name = _required_string(values, 'spillover_bucket_name', 'infra.oops.trails')
  unknown_placeholders = bucket_name.replace('{account}', '')
  if '{' in unknown_placeholders or '}' in unknown_placeholders:
    raise ValueError(
      'infra.oops.trails.spillover_bucket_name supports only the {account} placeholder'
    )
  return TrailsConfig(
    stack_name=_required_string(values, 'stack_name', 'infra.oops.trails'),
    repository=_required_string(values, 'repository', 'infra.oops.trails'),
    spillover_bucket_name=bucket_name,
    service_name=_required_string(values, 'service_name', 'infra.oops.trails'),
    load_balancer_ingress_logical_id=_required_string(
      values, 'load_balancer_ingress_logical_id', 'infra.oops.trails'
    ),
    load_balancer_egress_logical_id=_required_string(
      values, 'load_balancer_egress_logical_id', 'infra.oops.trails'
    ),
  )


def _with_defaults(
  defaults: Mapping[str, Any], overrides: Mapping[str, Any], path: str
) -> Mapping[str, Any]:
  _reject_unknown(overrides, set(defaults), path)
  return {**defaults, **overrides}


def _optional_mapping(raw: Mapping[str, Any], key: str, path: str) -> Mapping[str, Any]:
  value = raw.get(key, {})
  return _mapping(value, f'{path}.{key}')


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
  if not isinstance(value, dict):
    raise ValueError(f'{path} must be an object')
  return value


def _required_string(raw: Mapping[str, Any], key: str, path: str) -> str:
  if key not in raw:
    raise ValueError(f'{path}.{key} is required')
  value = raw[key]
  if not isinstance(value, str) or value == '':
    raise ValueError(f'{path}.{key} must be a non-empty string')
  return value


def _nullable_string(raw: Mapping[str, Any], key: str, path: str) -> Optional[str]:
  if raw.get(key) is None:
    return None
  return _required_string(raw, key, path)


def _optional_string(raw: Mapping[str, Any], key: str, default: str, path: str) -> str:
  if key not in raw:
    return default
  return _required_string(raw, key, path)


def _reject_unknown(raw: Mapping[str, Any], expected: set[str], path: str) -> None:
  unknown = set(raw) - expected
  if len(unknown) > 0:
    names = ', '.join(sorted(unknown))
    raise ValueError(f'{path} has unknown fields: {names}')
