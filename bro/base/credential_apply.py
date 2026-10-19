"""Staged credential publication with an indexed material commit point."""

import base64
import contextlib
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bro.base import credentials
from bro.base.credential_environment import ENVIRONMENT_FILE, render

STATE_FILE = '.credential-hooks.json'


def atomic_file(path: Path, content: bytes, mode: int = 0o600) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  descriptor, name = tempfile.mkstemp(dir=path.parent, prefix='.publish-')
  temporary = Path(name)
  with contextlib.ExitStack() as cleanup:
    cleanup.callback(temporary.unlink, missing_ok=True)
    with os.fdopen(descriptor, 'wb') as stream:
      os.fchmod(stream.fileno(), mode)
      stream.write(content)
      stream.flush()
      os.fsync(stream.fileno())
    os.replace(temporary, path)


def hook_plan(
  registry: Mapping[str, credentials.CredentialKind],
  kinds: set[str],
  store: credentials.Store,
  directory: Path,
  environment: Mapping[str, str],
) -> dict[str, Any]:
  result = {}
  for kind in sorted(kinds):
    if kind not in registry:
      raise ValueError(f'install hook requested for unknown credential kind {kind!r}')
    store.get(kind)
    hook = registry[kind].install
    files, variables = {}, {}
    if hook is not None:
      for path, value in hook.get('files', {}).items():
        files[path] = [credentials._install_value(value, directory, store), 0o600]
      for variable, value in hook.get('env', {}).items():
        variables[variable] = credentials._install_value(value, directory, store)
      for command, specification in hook.get('commands', {}).items():
        files['bin/' + command] = [
          credentials._command_wrapper(
            kind, command, specification.get('env', {}), directory, environment, store
          ),
          0o700,
        ]
    result[kind] = {'files': files, 'env': variables}
  return result


def publish_hooks(directory: Path, old: dict, new: dict) -> dict[str, str]:
  owners: dict[str, str] = {}
  variables: dict[str, str] = {}
  for kind, hook in new.items():
    for path in hook['files']:
      credentials._claim(owners, 'file ' + path, kind)
    for variable, value in hook['env'].items():
      credentials._claim(owners, 'variable ' + variable, kind)
      variables[variable] = value
  for kind, hook in new.items():
    if old.get(kind) == hook:
      continue
    for path, (value, mode) in hook['files'].items():
      atomic_file(directory / path, value.encode(), mode)
  previous_variables = {name for hook in old.values() for name in hook['env']}
  environment: dict[str, str | None] = dict.fromkeys(previous_variables - variables.keys())
  environment_path = directory / ENVIRONMENT_FILE
  if environment_path.exists():
    import shlex

    tokens = shlex.split(environment_path.read_text())
    environment.update(
      {name: None for verb, name in zip(tokens[::2], tokens[1::2], strict=True) if verb == 'unset'}
    )
  environment.update(variables)
  atomic_file(environment_path, render(environment).encode())
  new_paths = {path for hook in new.values() for path in hook['files']}
  for path in {path for hook in old.values() for path in hook['files']} - new_paths:
    (directory / path).unlink(missing_ok=True)
  atomic_file(directory / STATE_FILE, json.dumps(new).encode())
  return variables


def install(
  registry: Mapping[str, credentials.CredentialKind],
  kinds: set[str],
  store: credentials.Store,
  directory: Path,
  environment: Mapping[str, str],
) -> dict[str, str]:
  if directory.exists():
    shutil.rmtree(directory)
  directory.mkdir(parents=True, mode=0o700)
  (directory / 'bin').mkdir(mode=0o700)
  plan = hook_plan(registry, kinds, store, directory, environment)
  variables = publish_hooks(directory, {}, plan)
  variables['PATH'] = os.pathsep.join([str(directory / 'bin'), environment.get('PATH', '')])
  return variables


def stage(
  store: Path,
  directory: Path,
  files: Mapping[str, bytes],
  changed: set[str],
  operation: str,
  environment: Mapping[str, str],
) -> dict[str, Any]:
  current = json.loads((store / credentials.STORE_FILE).read_text())
  incoming = json.loads(files[credentials.STORE_FILE])
  index = dict(current)
  for field in ('material', 'sources'):
    index[field] = {
      name: value
      for name, value in current[field].items()
      if credentials.parse_name(name)[0] not in changed
    }
  additions = {}
  for name, filename in incoming['material'].items():
    if name in index['material']:
      if credentials.parse_name(name)[0] not in changed:
        continue
    destination = f'{credentials.MATERIAL_DIR}/{uuid.uuid4().hex}{credentials.MATERIAL_SUFFIX}'
    index['material'][name] = destination
    additions[destination] = base64.b64encode(files[filename]).decode()
    if name in incoming['sources']:
      index['sources'][name] = incoming['sources'][name]
  defaults = {
    credentials.parse_name(name)[0]: name
    for name in current['defaults']
    if credentials.parse_name(name)[0] not in changed
  }
  for name in incoming['defaults']:
    kind, _ = credentials.parse_name(name)
    if kind in changed or kind not in defaults:
      defaults[kind] = name
  index['defaults'] = sorted(defaults.values())
  index['kinds'] = sorted((set(current['kinds']) - changed) | set(incoming['kinds']))
  index['generation'] = current['generation'] + 1
  index['operation'] = operation
  with tempfile.TemporaryDirectory(prefix='credential-stage-') as temporary:
    temporary_store = Path(temporary)
    for filename, value in files.items():
      atomic_file(temporary_store / filename, value)
    source = credentials.Store(credentials.default_registry(), temporary_store, {})
    old = json.loads((directory / STATE_FILE).read_text())
    new = {kind: hook for kind, hook in old.items() if kind not in changed}
    new.update(hook_plan(source.registry, set(incoming['kinds']), source, directory, environment))
    validation = temporary_store / 'merged'
    atomic_file(validation / credentials.STORE_FILE, json.dumps(index).encode())
    credentials.Store(source.registry, validation, {})
    owners: dict[str, str] = {}
    for kind, hook in new.items():
      for path in hook['files']:
        credentials._claim(owners, 'file ' + path, kind)
      for name in hook['env']:
        credentials._claim(owners, 'variable ' + name, kind)
  return {'index': index, 'material': additions, 'old_hooks': old, 'hooks': new}


def apply(store: Path, directory: Path, transaction: dict[str, Any]) -> int:
  index = transaction['index']
  current = json.loads((store / credentials.STORE_FILE).read_text())
  (store / credentials.MATERIAL_DIR).mkdir(exist_ok=True)
  if current.get('operation') != index['operation']:
    if current['generation'] + 1 != index['generation']:
      raise ValueError('credential transaction has a superseded generation')
    for filename, value in transaction['material'].items():
      atomic_file(store / filename, base64.b64decode(value, validate=True))
    atomic_file(store / credentials.STORE_FILE, json.dumps(index).encode())
  publish_hooks(directory, transaction['old_hooks'], transaction['hooks'])
  retained = set(index['material'].values())
  for path in (store / credentials.MATERIAL_DIR).iterdir():
    if path.is_file() and str(path.relative_to(store)).removesuffix('.minted') not in retained:
      path.unlink(missing_ok=True)
  return index['generation']


def apply_request(store: Path, directory: Path, request: dict[str, Any]) -> int:
  import fcntl
  import time

  from bro.base import log

  with (store / '.apply.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    operation = request['operation']
    if (
      not isinstance(operation, str)
      or not operation
      or any(character not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for character in operation)
    ):
      raise ValueError('invalid credential operation id')
    completed_path = store / '.operations' / (operation + '.done.json')
    if completed_path.exists():
      return json.loads(completed_path.read_text())['generation']
    transaction_path = store / '.operations' / (operation + '.json')
    if transaction_path.exists():
      transaction = json.loads(transaction_path.read_text())
    else:
      files = {
        name: base64.b64decode(value, validate=True) for name, value in request['files'].items()
      }
      transaction = stage(store, directory, files, set(request['changed']), operation, os.environ)
      atomic_file(transaction_path, json.dumps(transaction).encode())
    while True:
      try:
        generation = apply(store, directory, transaction)
        break
      except OSError as error:
        current = json.loads((store / credentials.STORE_FILE).read_text())
        if current.get('operation') != operation:
          raise
        log.warning('retrying committed credential publication %s: %s', operation, error)
        time.sleep(0.1)
    atomic_file(completed_path, json.dumps({'generation': generation}).encode())
    transaction_path.unlink()
    return generation
