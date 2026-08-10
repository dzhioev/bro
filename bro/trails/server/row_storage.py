"""Prepare service rows and spill objects from harness-native records."""

import asyncio
from typing import Any

from bro.trails import backends, rows
from bro.trails.server import storage_types


async def prepare_rows(
  *,
  s3,
  bucket: str,
  trail_id: str,
  offset: int,
  payloads: list[Any],
  adapter: backends.Adapter,
  default_timestamp: str,
  state: rows.AggregateState,
  seen_billing_keys: set[str],
) -> list[dict]:
  prepared = rows.build_rows(
    trail_id=trail_id,
    offset=offset,
    payloads=payloads,
    adapter=adapter,
    default_timestamp=default_timestamp,
    state=state,
    seen_billing_keys=seen_billing_keys,
  )
  for row in prepared:
    body = row.pop('body')
    body_payload = storage_types.body_bytes(body)
    if len(body_payload) > storage_types.MAX_BODY_BYTES:
      raise storage_types.BodyTooLarge(
        f'body size {len(body_payload)} exceeds {storage_types.MAX_BODY_BYTES}'
      )
    if len(body_payload) < storage_types.SPILLOVER_THRESHOLD_BYTES:
      row['body'] = body
      continue
    key = storage_types.universal_spillover_key(trail_id, row['step_id'], body_payload)
    await asyncio.to_thread(
      s3.put_object,
      Bucket=bucket,
      Key=key,
      Body=body_payload,
      ContentType='application/json',
    )
    row['body_s3'] = key
    row['body_encoding'] = 'text' if isinstance(body, str) else 'json'
  return prepared
