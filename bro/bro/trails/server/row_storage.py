"""Prepare universal rows and their spill objects from harness-native records."""

import asyncio
from typing import Any

from trails.model import canonical_json_bytes
from trails.server import backends, storage_types
from trails.server.folding import AggregateState


async def prepare_rows(
  *,
  s3,
  bucket: str,
  trail_id: str,
  offset: int,
  payloads: list[Any],
  adapter: backends.Adapter,
  default_timestamp: str,
  state: AggregateState,
  seen_billing_keys: set[str],
) -> list[dict]:
  rows: list[dict] = []
  for index, payload in enumerate(payloads, start=offset):
    parsed = adapter.parse(payload)
    classification = adapter.classify(parsed)
    contribution = state.apply(parsed, classification, seen_billing_keys)
    row: dict[str, Any] = {
      'trail_id': trail_id,
      'step_id': index,
      'ts': parsed.timestamp if parsed.timestamp is not None else default_timestamp,
      'kind': parsed.kind,
      'payload_sha256': storage_types.sha256_hex(canonical_json_bytes(payload)),
      **parsed.attributes,
    }
    if contribution is not None:
      row['usage'] = contribution
    body_payload = storage_types.body_bytes(parsed.body)
    if len(body_payload) > storage_types.MAX_BODY_BYTES:
      raise storage_types.BodyTooLarge(
        f'body size {len(body_payload)} exceeds {storage_types.MAX_BODY_BYTES}'
      )
    if len(body_payload) >= storage_types.SPILLOVER_THRESHOLD_BYTES:
      key = storage_types.universal_spillover_key(trail_id, index, body_payload)
      await asyncio.to_thread(
        s3.put_object,
        Bucket=bucket,
        Key=key,
        Body=body_payload,
        ContentType='application/json',
      )
      row['body_s3'] = key
      row['body_encoding'] = 'text' if isinstance(parsed.body, str) else 'json'
    else:
      row['body'] = parsed.body
    rows.append(row)
  return rows
