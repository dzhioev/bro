"""Local trails data attached to container launch descriptions."""

from bro.base import credentials
from bro.trails.store import local_root, selects_local_storage
from bro.workspace.paths import CONTAINER_TRAILS_ROOT
from ride.scope import credential_store
from ride.workspace.store import ScopedSecrets


def local_trails_mounts(scoped: ScopedSecrets) -> tuple[str, ...]:
  """the host-root bind a launch under `scoped` needs to record locally — empty
  where its scope resolves a trails credential selecting another backend. the
  caller owns the recording decision itself: a launch that disables recording
  must not ask.
  """
  view = credentials.scoped_view_store(
    credential_store(scoped), scoped.required, optional=scoped.optional
  )
  if not selects_local_storage(view):
    return ()
  host_root = local_root().resolve()
  host_root.mkdir(parents=True, exist_ok=True)
  return (f'{host_root}:{CONTAINER_TRAILS_ROOT}',)
