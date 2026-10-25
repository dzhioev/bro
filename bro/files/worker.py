"""One prepared provider call in an irreversibly confined process."""

import encodings.utf_16_le  # noqa: F401
import faulthandler
import importlib
import json
import linecache  # noqa: F401
import os
import sys
import traceback
from typing import cast

from bro.files import confinement
from bro.files.confinement import SandboxLimits
from bro.files.protocol import (
  MAX_REPLY_BYTES,
  MAX_REQUEST_BYTES,
  InvalidInput,
  Provider,
  encode_frame,
  read_exact,
  read_frame,
)
from bro.files.text import validate_strings
from bro.files.types import Limits


def _load(spec: str) -> Provider:
  module, attribute = spec.split(':')
  value = importlib.import_module(module)
  for name in attribute.split('.'):
    value = getattr(value, name)
  return cast(Provider, value)


def run_worker() -> None:
  reply_descriptor = 1
  phase = 'engine'
  try:
    spec, parent_pid, sandbox_json, type_limits_json = sys.argv[1:]
    sandbox_limits = SandboxLimits(**json.loads(sandbox_json))
    limits = Limits(**json.loads(type_limits_json))
    phase = 'unavailable'
    confinement.architecture()
    importlib.import_module('bro.files.confinement.linux')
    faulthandler.enable(file=sys.stderr)
    phase = 'engine'
    provider = _load(spec)
    engine = provider.prepare()
    if type(engine) is not str or not engine:
      raise TypeError('prepare must return an engine version string')
    validate_strings(engine)
    phase = 'unavailable'
    confinement.arm_parent_death(int(parent_pid), sandbox_limits)
    reply_descriptor = os.dup(1)
    os.dup2(2, 1)
    report = confinement.apply(sandbox_limits)
    phase = 'engine'
    request = read_frame(sys.stdin.buffer, MAX_REQUEST_BYTES)
    if not isinstance(request, dict) or request.keys() != {
      'operation',
      'arguments',
      'snapshot_size_bytes',
    }:
      raise ValueError('invalid request shape')
    size = request['snapshot_size_bytes']
    if type(size) is not int or not 0 <= size <= limits.max_bytes:
      raise ValueError('invalid snapshot size')
    snapshot = read_exact(sys.stdin.buffer, size)
    arguments = request['arguments']
    if request['operation'] == 'info' and arguments == {}:
      result = provider.info(snapshot, limits)
    elif (
      request['operation'] == 'view'
      and isinstance(arguments, dict)
      and arguments.keys() == {'view', 'pages'}
    ):
      result = provider.view(snapshot, arguments['view'], arguments['pages'], limits)
    else:
      raise ValueError('invalid provider call')
    response = {'ok': result, 'engine': engine, 'sandbox': report}
  except InvalidInput as error:
    response = {'error': {'kind': 'invalid_input', 'message': str(error)}}
  except Exception as error:
    traceback.print_exc(file=sys.stderr)
    message = str(error) if phase == 'unavailable' else 'file engine failure'
    response = {'error': {'kind': phase, 'message': message}}
  try:
    frame = encode_frame(response)
    if len(frame) > MAX_REPLY_BYTES:
      frame = encode_frame(
        {'error': {'kind': 'engine', 'message': 'file reply exceeds its byte limit'}}
      )
    with os.fdopen(reply_descriptor, 'wb', buffering=0) as reply:
      position = 0
      while position < len(frame):
        position += reply.write(frame[position:])
  except Exception:
    traceback.print_exc(file=sys.stderr)
  os._exit(0)


if __name__ == '__main__':
  run_worker()
