"""Local trails data attached to container launch descriptions."""

from bro.base import credentials
from bro.trails.store import local_root
from bro.workspace.store import ScopedSecrets

_CONTAINER_TRAILS_ROOT = '/home/cw/.bro-trails'


def local_trails_launch_data(
  scoped: ScopedSecrets,
) -> tuple[dict[str, str], tuple[str, ...]]:
  trails_names = {name for name in scoped.required if credentials.parse_name(name)[0] == 'trails'}
  if len(trails_names) == 0:
    return {}, ()
  if len(trails_names) > 1:
    raise ValueError('a launch scope may select only one trails credential')
  store = credentials.scoped_view_store(scoped.required, optional=scoped.optional)
  if store.get_json('trails').get('backend', 'service') != 'local':
    return {}, ()
  host_root = local_root().resolve()
  host_root.mkdir(parents=True, exist_ok=True)
  return (
    {'BRO_TRAILS_DIR': _CONTAINER_TRAILS_ROOT},
    (f'{host_root}:{_CONTAINER_TRAILS_ROOT}',),
  )
