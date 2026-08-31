import importlib.metadata
import os
from functools import cache

# The exclusive credential store selected for this process.
DEFAULT_STORE_DIR = os.path.expanduser('~/.bro')
STORE_DIR = os.environ.get('BRO_STORE', DEFAULT_STORE_DIR)
if STORE_DIR == '':
  raise ValueError('BRO_STORE must not be empty')

# The host's own launch policy, beside the store rather than inside it.
DEFAULT_HOST_CONFIG = os.path.expanduser('~/.bro.json')
DEFAULT_SUMMON_DEPTH = 2

# The framework's own distribution.
# `bro` is a namespace package several distributions ship portions of, so the
# one whose version is the framework revision has to be named explicitly.
DISTRIBUTION = 'bro'


@cache
def _distribution_version() -> str:
  return importlib.metadata.version(DISTRIBUTION)


VERSION: str


def __getattr__(name: str):
  if name == 'VERSION':
    return _distribution_version()
  raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
