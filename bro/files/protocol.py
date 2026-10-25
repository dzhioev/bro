"""Provider calls and length-prefixed JSON messages."""

import json
import math
from dataclasses import dataclass
from typing import Any, BinaryIO, Literal, Protocol

from bro.files.confinement import SandboxReport
from bro.files.types import FileType, Limits

MAX_REPLY_BYTES = 8 * 1024**2
MAX_STDERR_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 64 * 1024


class InvalidInput(ValueError):
  """A document or selection fault, described only with fixed phrases and counts."""


class Provider(Protocol):
  def prepare(self) -> str: ...
  def info(self, snapshot: bytes, limits: Limits) -> dict[str, Any]: ...
  def view(
    self, snapshot: bytes, view: str, pages: list[int] | None, limits: Limits
  ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Job:
  file_type: FileType
  operation: Literal['info', 'view']
  arguments: dict[str, Any]
  snapshot: bytes

  def __post_init__(self) -> None:
    if self.operation == 'info':
      if self.arguments:
        raise ValueError('info takes no provider arguments')
    elif self.operation == 'view':
      if self.arguments.keys() != {'view', 'pages'}:
        raise ValueError('view requires view and pages')
      self.file_type.get_view(self.arguments['view'])
    else:
      raise ValueError('unknown file operation')


@dataclass(frozen=True)
class Reply:
  """A runner's payload, validated and serialized against the declared model."""

  ok: dict[str, Any]
  engine: str
  sandbox: SandboxReport


def encode_frame(value: object) -> bytes:
  data = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode(
    'utf-8'
  )
  return len(data).to_bytes(8, 'big') + data


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
  result = {}
  for key, value in pairs:
    if key in result:
      raise ValueError('duplicate JSON key')
    result[key] = value
  return result


def _constant(value: str) -> None:
  raise ValueError('nonfinite JSON number')


def _float(value: str) -> float:
  result = float(value)
  if not math.isfinite(result):
    raise ValueError('nonfinite JSON number')
  return result


def decode_json(data: bytes) -> Any:
  return json.loads(
    data.decode('utf-8'), object_pairs_hook=_object, parse_constant=_constant, parse_float=_float
  )


def read_exact(stream: BinaryIO, size: int) -> bytes:
  data = bytearray()
  while len(data) < size:
    chunk = stream.read(size - len(data))
    if not chunk:
      raise ValueError('incomplete frame')
    data.extend(chunk)
  return bytes(data)


def read_frame(stream: BinaryIO, maximum_bytes: int) -> Any:
  size = int.from_bytes(read_exact(stream, 8), 'big')
  if size > maximum_bytes:
    raise ValueError('frame exceeds its byte limit')
  return decode_json(read_exact(stream, size))
