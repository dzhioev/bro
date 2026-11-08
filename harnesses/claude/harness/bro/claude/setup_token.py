"""Mint the long-lived `claude_code` token with the harness's pinned Claude Code and
store it, so the host needs no Claude Code install of its own."""

import getpass
import os
import shlex
import subprocess
import sys
import tempfile
from typing import Optional

from bro.base import credentials, log
from bro.base.args import Parser
from bro.claude import claude_release
from bro.claude.credentials import AUTH_KIND
from bro.workspace.paths import ISOLATION_ENV

__cli_name__ = 'claude-setup-token'

# what an OAuth token minted by `setup-token` opens with; an API key (`sk-ant-api…`)
# would bill sessions to the API instead of the subscription
_TOKEN_PREFIX = 'sk-ant-oat'


class SetupTokenError(Exception):
  pass


def setup_command(stored_name: str) -> str:
  """the command line that mints and stores `stored_name`, an instance of the kind."""
  kind, instance = credentials.parse_name(stored_name)
  if kind != AUTH_KIND:
    raise ValueError(f'{stored_name!r} is no {AUTH_KIND} instance')
  if instance is None or instance == '':
    return __cli_name__
  return shlex.join([__cli_name__, '--instance', instance])


def _checked(token: str) -> str:
  if not token.startswith(_TOKEN_PREFIX) or any(character.isspace() for character in token):
    raise SetupTokenError(
      f'the pasted text is not a setup token: one opens with {_TOKEN_PREFIX} and holds no spaces'
    )
  return token


def mint(instance: str) -> str:
  """run the pinned `claude setup-token` on this terminal, read the token it shows at a
  hidden prompt, and write it as the `instance` of the kind; the summary of what was
  stored."""
  if ISOLATION_ENV in os.environ:
    raise SetupTokenError(f'{__cli_name__} must run on the host, outside a managed session')
  if not sys.stdin.isatty():
    raise SetupTokenError(f'{__cli_name__} requires a terminal on stdin')
  name = credentials.storage_name(AUTH_KIND, instance)
  try:
    credentials.parse_name(name)
    platform_name = claude_release.host_platform()
  except ValueError as error:
    raise SetupTokenError(str(error)) from error
  store = credentials.Store(credentials.default_registry(), credentials.STORE_DIR, {})
  if store.source_type(name) != credentials.LocalSource.TYPE:
    raise SetupTokenError(f'{name!r} has a non-local credential source')
  from bro.claude.claude_config import CLAUDE_CONFIG_DIR_ENV
  from bro.claude.provisioning import claude_code_version

  binary = claude_release.cached_binary(claude_code_version(), platform_name)
  try:
    with (
      store.stored_name_lock(name, blocking=False),
      tempfile.TemporaryDirectory(prefix='claude-setup-token-') as config_directory,
    ):
      expected = store.read_stored_material(name)
      # setup-token writes a `.claude.json` into its config directory, and a host one
      # lacking the account identity aborts every later launch; the pinned release
      # must not update itself
      environment = {
        **os.environ,
        CLAUDE_CONFIG_DIR_ENV: config_directory,
        'DISABLE_AUTOUPDATER': '1',
      }
      status = subprocess.run([str(binary), 'setup-token'], env=environment).returncode
      if status != 0:
        raise SetupTokenError(f'claude setup-token exited with status {status}')
      token = _checked(getpass.getpass('paste the token setup-token showed: ').strip())
      store.write_stored_material(name, token.encode(), expected=expected)
  except (credentials.StoredNameLocked, credentials.StoredMaterialChanged) as error:
    raise SetupTokenError(str(error)) from error
  action = 'replaced' if expected is not None else 'stored'
  # the `kind+instance` spelling addresses storage, where the bare kind would resolve
  # the store's default pick
  return f'{action} {name} at {store.material_path(f"{AUTH_KIND}+{instance}")}'


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(
    description='mint a long-lived Claude Code token with the pinned Claude Code and store it'
  )
  parser.add_argument(
    '--instance', default='', help=f'store it as {AUTH_KIND}+INSTANCE (default: {AUTH_KIND})'
  )
  args = parser.parse(argv)
  try:
    print(mint(args['instance']))
  except SetupTokenError as error:
    log.error('%s', error)
    return 1
  return None
