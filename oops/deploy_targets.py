from bro.oops.targets import Command, DeployTarget, ECSService, HTTPProbe, TargetRegistry


def _targets() -> dict[str, DeployTarget]:
  from bro.oops.cdk.config import resolve

  config = resolve()
  return {
    'trails-server': DeployTarget(
      deploy=Command('oops/trails/server/deploy.sh'),
      verify=Command('oops/trails/server/verify.sh'),
      ecs=ECSService(
        region=config.region,
        cluster=config.platform.cluster_name,
        service=config.trails.service_name,
      ),
      probe=HTTPProbe(f'https://trails.{config.delegated_subdomain}/health'),
      paths=('bro/trails/', 'oops/'),
      notes='the deploy command rolls the configured trails image and service stacks',
    )
  }


registry = TargetRegistry(
  load_targets=_targets,
  needed_secrets=('aws', 'github', 'infra'),
)
