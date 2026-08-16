"""Local trails data attached to container launch descriptions."""

from bro.base import credentials
from bro.trails.store import local_root, selects_local_storage
from bro.workspace.store import ScopedSecrets

# the container's project root is its /workspace clone, so the host root binds
# where an in-container `local_root()` already looks
_CONTAINER_TRAILS_ROOT = '/workspace/var/cw/trails'


def local_trails_mounts(scoped: ScopedSecrets) -> tuple[str, ...]:
  """the host-root bind a launch under `scoped` needs to record locally — empty
  where its scope resolves a trails credential selecting another backend. the
  caller owns the recording decision itself: a launch that disables recording
  must not ask.
  """
  view = credentials.scoped_view_store(scoped.required, optional=scoped.optional)
  if not selects_local_storage(view):
    return ()
  host_root = local_root().resolve()
  host_root.mkdir(parents=True, exist_ok=True)
  return (f'{host_root}:{_CONTAINER_TRAILS_ROOT}',)
