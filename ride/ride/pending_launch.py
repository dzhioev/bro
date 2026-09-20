"""Pending records for manually launched workers."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bro.workspace.paths import is_workspace_name, launch_dir
from ride.session_env import env_additions


class UnknownToken(Exception):
  """No pending launch exists behind a token."""


@dataclass(frozen=True)
class PendingLaunch:
  token: str
  runtime: str
  port: int
  channel_token: str
  type: str
  talk: tuple[str, ...]
  owner_tree: str
  env: dict[str, str] = field(default_factory=dict)
  extension: dict[str, Any] = field(default_factory=dict)

  def address(self, host: str | None = None) -> str:
    from bro.broker.transports.tcp import LOCAL_HOST, Endpoint

    return Endpoint(port=self.port, token=self.channel_token).address(host or LOCAL_HOST)


def _path(token: str) -> Path:
  return launch_dir() / 'pending' / f'{token}.json'


def _claimed_path(token: str) -> Path:
  return launch_dir() / 'claimed' / f'{token}.json'


def _atomic_write(path: Path, value: str) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.')
  temporary = Path(temporary_name)
  try:
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
      stream.write(value)
    os.replace(temporary, path)
  except BaseException:
    temporary.unlink(missing_ok=True)
    raise


def write(pending: PendingLaunch) -> None:
  _atomic_write(_path(pending.token), json.dumps(asdict(pending), ensure_ascii=False, indent=2))


def _read_data(token: str) -> dict[str, Any]:
  try:
    data = json.loads(_path(token).read_text())
  except FileNotFoundError:
    raise UnknownToken(
      f'no pending manual launch for token {token!r}: never registered, '
      'already claimed, or its mission ended'
    ) from None
  if not isinstance(data, dict):
    raise ValueError(f'pending manual launch {token!r} is not a JSON object')
  return data


def _runtime_value(data: dict[str, Any], token: str) -> str:
  runtime = data.get('runtime')
  if not isinstance(runtime, str) or runtime == '':
    raise ValueError(f'pending manual launch {token!r} carries no usable runtime')
  from ride.runtime_bundle import RuntimeBundleError, runtime_root_from_reference

  try:
    runtime_root_from_reference(runtime)
  except RuntimeBundleError as error:
    raise ValueError(
      f'pending manual launch {token!r} carries no usable runtime: {error}'
    ) from error
  return runtime


def runtime_reference(token: str) -> str:
  return _runtime_value(_read_data(token), token)


def peek(token: str) -> PendingLaunch:
  data = _read_data(token)
  _runtime_value(data, token)
  talk_values = data.get('talk')
  if not isinstance(talk_values, list) or not all(isinstance(value, str) for value in talk_values):
    raise ValueError(f'pending manual launch {token!r} carries invalid talk')
  from bro.broker.brotocol import decode_talk
  from bro.worker_types import type_name

  try:
    decode_talk(','.join(talk_values))
    worker_type = data.get('type')
    if not isinstance(worker_type, str):
      raise ValueError('worker type must be a string')
    type_name(worker_type)
    env_additions(data.get('env'))
  except (TypeError, ValueError) as error:
    raise ValueError(f'pending manual launch {token!r} is invalid: {error}') from error
  owner_tree = data.get('owner_tree')
  if not isinstance(owner_tree, str) or owner_tree == '':
    raise ValueError(f'pending manual launch {token!r} carries no owner tree')
  extension = data.get('extension')
  if not isinstance(extension, dict):
    raise ValueError(f'pending manual launch {token!r} carries no extension object')
  loaded = PendingLaunch(**{**data, 'talk': tuple(talk_values)})
  if loaded.token != token:
    raise ValueError(f'pending launch record {token!r} names token {loaded.token!r}')
  return loaded


def claim(token: str, *, workspace: str) -> PendingLaunch:
  pending = peek(token)
  try:
    _path(token).unlink()
  except FileNotFoundError:
    raise UnknownToken(
      f'pending manual launch {token!r} was just claimed by another launch'
    ) from None
  _atomic_write(
    _claimed_path(token),
    json.dumps({'token': token, 'workspace': workspace}, ensure_ascii=False),
  )
  return pending


def claimed_workspace(token: str) -> str | None:
  try:
    data = json.loads(_claimed_path(token).read_text())
  except FileNotFoundError:
    return None
  workspace = data.get('workspace')
  if not isinstance(workspace, str) or not is_workspace_name(workspace):
    raise ValueError(f'claimed launch record {token!r} carries no usable workspace name')
  return workspace


def discard(token: str) -> None:
  _path(token).unlink(missing_ok=True)
  _claimed_path(token).unlink(missing_ok=True)
