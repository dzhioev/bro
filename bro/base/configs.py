import importlib.metadata
import os
from functools import cache

BRO_CONFIGS_DIR = os.environ.get('BRO_CONFIGS_DIR')
if BRO_CONFIGS_DIR == '':
  raise ValueError('BRO_CONFIGS_DIR must not be empty')

# the standalone host secret store; host-side writable credential caches write
# here directly.
DEFAULT_BRO_DIR = os.path.expanduser('~/.bro')

# the host's own launch policy, beside the store rather than inside it: config,
# not secrets.
DEFAULT_HOST_CONFIG = os.path.expanduser('~/.bro.json')


# the framework's own distribution. `bro` is a namespace package several
# distributions ship portions of, so the one whose version is the framework
# revision has to be named rather than inferred from package ownership.
DISTRIBUTION = 'bro'


@cache
def _distribution_version() -> str:
  return importlib.metadata.version(DISTRIBUTION)


VERSION: str


def __getattr__(name: str):
  if name == 'VERSION':
    return _distribution_version()
  raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
