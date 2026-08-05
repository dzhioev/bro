import importlib.metadata
import os
from functools import cache

BRO_CONFIGS_DIR = os.environ.get('BRO_CONFIGS_DIR')
if BRO_CONFIGS_DIR == '':
  raise ValueError('BRO_CONFIGS_DIR must not be empty')

# the standalone host secret store; host-side writable credential caches write
# here directly.
DEFAULT_BRO_DIR = os.path.expanduser('~/.bro')


@cache
def _distribution_version() -> str:
  package_name = __name__.partition('.')[0]
  distribution_names = sorted(
    set(importlib.metadata.packages_distributions().get(package_name, []))
  )
  if len(distribution_names) != 1:
    raise RuntimeError(
      f'package {package_name!r} must belong to exactly one distribution, found '
      f'{distribution_names}'
    )
  return importlib.metadata.version(distribution_names[0])


VERSION: str


def __getattr__(name: str):
  if name == 'VERSION':
    return _distribution_version()
  raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
