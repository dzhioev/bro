"""Shared aggregate folding, row construction, and message projection."""

from collections.abc import Callable
from typing import Any, Optional

from bro.trails import backends
from bro.trails.lineage import LineageHead
from bro.trails.model import payload_sha256
from bro.trails.store import refusing_invalid_requests


class AggregateState:
  def __init__(self, header: dict, adapter: backends.Adapter):
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
    self.head = LineageHead.stored(native) if adapter.resolve_lineage is not None else None

  @classmethod
  def replaying(cls, header: dict, adapter: backends.Adapter) -> 'AggregateState':
    """The state a re-fold of a trail's whole row stream starts from: every field
    the fold derives is cleared, leaving what the trail was minted with."""
    native = {
      key: value
      for key, value in header.get('native', {}).items()
      if key not in backends.SERVER_DERIVED_NATIVE_FIELDS
    }
    native.update(replayed_native(adapter, header))
    return cls({'native': native, 'turn_count': 0}, adapter)

  def apply(
    self,
    record: backends.ParsedRecord,
    classification: backends.Classification,
    seen_billing_keys: set[str],
    *,
    step_id: int,
    digest: str,
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
    if self.head is not None:
      uuid = record.attributes.get('uuid')
      self.head.fold(
        step_id=step_id,
        uuid=uuid if isinstance(uuid, str) else None,
        payload_sha256=digest,
      )
      self.native['lineage_head'] = self.head.fields()
    return contribution


def inherited_native(adapter: backends.Adapter, parent: Callable[[], dict]) -> dict:
  """The native fields a fork of `parent` opens with: the conversation's first
  record, which no trail's rows carry once a history copy is skipped. The parent
  header is read only where the harness folds a head at all."""
  if adapter.resolve_lineage is None:
    return {}
  head = LineageHead.stored(parent().get('native', {})).inherited()
  return {'lineage_head': head.fields()}


def replayed_native(adapter: backends.Adapter, header: dict) -> dict:
  """The native fields a re-fold of a trail's own row stream starts from: what
  its fork inherited, plus the spans its mint awarded it."""
  if adapter.resolve_lineage is None:
    return {}
  head = LineageHead.stored(header.get('native', {})).replayed()
  return {'lineage_head': head.fields()}


def minted_native(native: dict, chunks: list[list[int]]) -> dict:
  """The native fields a trail leaves its mint with once a lineage verdict
  settled it: the artifact spans it was awarded, which none of its rows record."""
  head = LineageHead.stored(native)
  head.cuts = chunks
  return {'lineage_head': head.fields()}


def state_fields(state: AggregateState, extent: int) -> dict:
  """The header fields a folded aggregate contributes."""
  fields: dict[str, Any] = {
    'extent': extent,
    'turn_count': state.turn_count,
    'native': state.native,
  }
  if state.last_billed_message_id is not None:
    fields['last_billed_message_id'] = state.last_billed_message_id
  if state.subject is not None:
    fields['subject'] = state.subject
  return fields


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
    with refusing_invalid_requests(f'record at offset {step_id}'):
      parsed = adapter.parse(payload)
      classification = adapter.classify(parsed)
    digest = payload_sha256(payload)
    contribution = state.apply(
      parsed, classification, seen_billing_keys, step_id=step_id, digest=digest
    )
    row: dict[str, Any] = {
      'trail_id': trail_id,
      'step_id': step_id,
      'ts': parsed.timestamp if parsed.timestamp is not None else default_timestamp,
      'kind': parsed.kind,
      'payload_sha256': digest,
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
