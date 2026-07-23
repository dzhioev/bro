"""Shared storage constants, DynamoDB conversion, and storage exceptions."""

import decimal
import json
from datetime import UTC, datetime
from typing import Any, Optional

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from base.lulid import lulid

SPILLOVER_THRESHOLD_BYTES = 50 * 1024
MAX_BODY_BYTES = 10 * 1024 * 1024
INLINE_RESPONSE_THRESHOLD_BYTES = 1 * 1024 * 1024
PRESIGNED_URL_TTL_SECONDS = 3600

BRO_STEP_KINDS = (
  'system_prompt',
  'user_input',
  'reasoning',
  'assistant',
  'tool_call',
  'tool_result',
  'llm_call',
  'error',
  'end',
)

MESSAGE_TYPES = frozenset(
  {
    'user_input',
    'llm_call',
    'reasoning',
    'assistant',
    'tool_call',
    'tool_result',
    'system_prompt',
    'error',
    'end',
    'harness_event',
  }
)


class TrailNotFound(Exception):
  pass


class BodyTooLarge(Exception):
  pass


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


def body_size_bytes(body: Any) -> int:
  if body is None:
    return 0
  if isinstance(body, str):
    return len(body.encode('utf-8'))
  return len(json.dumps(body, ensure_ascii=False).encode('utf-8'))


def bro_spillover_key(trail_id: str, step_id: str) -> str:
  return f'trails/{trail_id}/steps/{step_id}.json'


def conditional_check_failed(exception) -> bool:
  reasons = getattr(exception, 'response', {}).get('CancellationReasons', [])
  return any(reason.get('Code') == 'ConditionalCheckFailed' for reason in reasons)


def cancellation_codes(exception) -> list[str]:
  reasons = getattr(exception, 'response', {}).get('CancellationReasons', [])
  return [reason.get('Code', 'None') for reason in reasons]
