"""Shared aggregate folding, row construction, and message projection."""

from typing import Any, Optional

from bro.trails import backends
from bro.trails.model import payload_sha256


class AggregateState:
  def __init__(self, header: dict):
    native = dict(header.get('native', {}))
    raw_usage = native.get('usage')
    self.usage = dict(raw_usage) if isinstance(raw_usage, dict) else {}
    raw_counts = native.get('step_counts_by_kind')
    self.counts = dict(raw_counts) if isinstance(raw_counts, dict) else {}
    self.native = native
    self.turn_count = int(header.get('turn_count', 0))
    last_billed = header.get('last_billed_message_id')
    self.last_billed_message_id = last_billed if isinstance(last_billed, str) else None
    self.subject = header.get('subject')

  def apply(
    self,
    record: backends.ParsedRecord,
    classification: backends.Classification,
    seen_billing_keys: set[str],
  ) -> Optional[dict]:
    if record.kind is not None:
      self.counts[record.kind] = int(self.counts.get(record.kind, 0)) + 1
    self.turn_count += classification.turn_delta
    if classification.native_updates is not None:
      self.native.update(classification.native_updates)
    if self.subject is None and classification.subject is not None:
      self.subject = classification.subject
    contribution: Optional[dict] = None
    if classification.usage_model is not None and classification.usage is not None:
      billing_key = classification.billing_key
      should_bill = billing_key is None or (
        billing_key != self.last_billed_message_id and billing_key not in seen_billing_keys
      )
      if should_bill:
        model = classification.usage_model
        previous = self.usage.get(model)
        self.usage[model] = backends.add_numeric_maps(
          previous if isinstance(previous, dict) else {}, classification.usage
        )
        contribution = classification.usage
        if billing_key is not None:
          seen_billing_keys.add(billing_key)
          self.last_billed_message_id = billing_key
    self.native['usage'] = self.usage
    self.native['step_counts_by_kind'] = self.counts
    return contribution


def build_rows(
  *,
  trail_id: str,
  offset: int,
  payloads: list[Any],
  adapter: backends.Adapter,
  default_timestamp: str,
  state: AggregateState,
  seen_billing_keys: set[str],
) -> list[dict]:
  result: list[dict] = []
  for step_id, payload in enumerate(payloads, start=offset):
    parsed = adapter.parse(payload)
    classification = adapter.classify(parsed)
    contribution = state.apply(parsed, classification, seen_billing_keys)
    row: dict[str, Any] = {
      'trail_id': trail_id,
      'step_id': step_id,
      'ts': parsed.timestamp if parsed.timestamp is not None else default_timestamp,
      'kind': parsed.kind,
      'payload_sha256': payload_sha256(payload),
      'body': parsed.body,
      **parsed.attributes,
    }
    if contribution is not None:
      row['usage'] = contribution
    result.append(row)
  return result


def project_messages(
  adapter: backends.Adapter, records: list[dict], types: Optional[set[str]] = None
) -> list[dict]:
  messages = [message for record in records for message in adapter.project(record)]
  undeclared = {message['type'] for message in messages} - adapter.emitted_message_types
  if len(undeclared) > 0:
    raise RuntimeError(f'adapter emitted undeclared message types: {sorted(undeclared)}')
  if types is not None:
    messages = [message for message in messages if message['type'] in types]
  return messages
