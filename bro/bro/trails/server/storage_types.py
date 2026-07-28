"""Shared storage constants, DynamoDB conversion, and storage exceptions."""

import decimal
import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Optional

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from base.lulid import lulid

SPILLOVER_THRESHOLD_BYTES = 50 * 1024
MAX_BODY_BYTES = 10 * 1024 * 1024
INLINE_RESPONSE_THRESHOLD_BYTES = 1 * 1024 * 1024
PRESIGNED_URL_TTL_SECONDS = 3600
# keeps both DynamoDB transaction limits: 51 operations and under 4 MB when every inline body
# is just below the spill threshold.
MAX_TRANSACTION_RECORDS = 50
UNIVERSAL_BODY_STORAGE = 'trail_steps_v2'


class TrailNotFound(Exception):
  pass


class BodyTooLarge(Exception):
  pass


class AppendConflict(Exception):
  def __init__(self, expected: int, actual: int):
    super().__init__(f'append offset {expected} does not match trail extent {actual}')
    self.expected = expected
    self.actual = actual


_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def new_id() -> str:
  return lulid()


def now_iso() -> str:
  return datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def normalise_float(value: Any) -> Any:
  if isinstance(value, float):
    return decimal.Decimal(str(value))
  if isinstance(value, list):
    return [normalise_float(item) for item in value]
  if isinstance(value, dict):
    return {key: normalise_float(item) for key, item in value.items()}
  return value


def ddb(value: Any) -> dict:
  return _serializer.serialize(normalise_float(value))


def ddb_item(item: dict) -> dict:
  return {key: ddb(value) for key, value in item.items()}


def normalise_decimal(value: Any) -> Any:
  if isinstance(value, decimal.Decimal):
    return int(value) if value == value.to_integral_value() else float(value)
  if isinstance(value, list):
    return [normalise_decimal(item) for item in value]
  if isinstance(value, dict):
    return {key: normalise_decimal(item) for key, item in value.items()}
  return value


def from_ddb(value: dict) -> Any:
  return normalise_decimal(_deserializer.deserialize(value))


def from_ddb_item(item: Optional[dict]) -> Optional[dict]:
  if item is None:
    return None
  return {key: from_ddb(value) for key, value in item.items()}


def body_bytes(body: Any) -> bytes:
  if isinstance(body, bytes):
    return body
  if isinstance(body, str):
    return body.encode('utf-8')
  return json.dumps(body, ensure_ascii=False).encode('utf-8')


def body_size_bytes(body: Any) -> int:
  if body is None:
    return 0
  return len(body_bytes(body))


def sha256_hex(payload: bytes) -> str:
  return hashlib.sha256(payload).hexdigest()


def universal_spillover_key(trail_id: str, step_id: int, payload: bytes) -> str:
  return f'trails/steps/{trail_id}/{step_id}-{sha256_hex(payload)}.json'


def tool_blob_key(sha256: str) -> str:
  return f'trails/tools/{sha256}.json'


def context_key(trail_id: str) -> str:
  return f'trails/{trail_id}/context.json'


def relink_manifest_key(trail_id: str, timestamp: str) -> str:
  compact = timestamp.replace(':', '').replace('.', '')
  return f'trails/migrations/relink/{trail_id}-{compact}.json'


def cancellation_codes(exception) -> list[str]:
  reasons = getattr(exception, 'response', {}).get('CancellationReasons', [])
  return [reason.get('Code', 'None') for reason in reasons]
