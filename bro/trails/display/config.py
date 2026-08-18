"""Immutable display configuration and the named preset registry."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from bro.trails.display.records import (
  ALL_RECORD_KINDS,
  CONVERSATION_RECORD_KINDS,
  STRUCTURAL_RECORD_KINDS,
  DisplayRecord,
  RecordKind,
)


class Verbosity(StrEnum):
  COMPACT = 'compact'
  NORMAL = 'normal'
  FULL = 'full'
  DEBUG = 'debug'


class Layout(StrEnum):
  EVENT_LOG = 'event-log'
  CONVERSATION = 'conversation'
  NATIVE_STEPS = 'native-steps'
  TRAIL_LIST = 'trail-list'
  LINEAGE_TREE = 'lineage-tree'


class ColorMode(StrEnum):
  AUTO = 'auto'
  ALWAYS = 'always'
  NEVER = 'never'


class Appearance(StrEnum):
  PLAIN_LOG = 'plain-log'
  CHAT = 'chat'
  REWIND = 'rewind'


class TimestampPolicy(StrEnum):
  HIDDEN = 'hidden'
  WHEN_KNOWN = 'when-known'
  PLACEHOLDER = 'placeholder'


class OutputRoute(StrEnum):
  CONVERSATION = 'conversation'
  REPLY = 'reply'
  TRACE = 'trace'
  METADATA = 'metadata'
  STATUS = 'status'


@dataclass(frozen=True)
class AttributePredicate:
  """An equality test over a record attribute, including dotted nested attributes."""

  attribute: str
  value: Any

  def __post_init__(self) -> None:
    if len(self.attribute) == 0 or any(len(part) == 0 for part in self.attribute.split('.')):
      raise ValueError('filter attribute path must contain non-empty names')

  def matches(self, record: DisplayRecord) -> bool:
    current: Any = record
    for part in self.attribute.split('.'):
      if not hasattr(current, part):
        return False
      current = getattr(current, part)
    return current == self.value


@dataclass(frozen=True)
class RecordFilter:
  included_kinds: frozenset[RecordKind] | None = None
  excluded_kinds: frozenset[RecordKind] = frozenset()
  predicates: tuple[AttributePredicate, ...] = ()

  def __post_init__(self) -> None:
    if self.included_kinds is not None:
      overlap = self.included_kinds & self.excluded_kinds
      if len(overlap) > 0:
        raise ValueError(f'record filter includes and excludes {sorted(overlap)}')

  @classmethod
  def including(cls, *kinds: RecordKind) -> 'RecordFilter':
    return cls(included_kinds=frozenset(kinds))

  @classmethod
  def excluding(cls, *kinds: RecordKind) -> 'RecordFilter':
    return cls(excluded_kinds=frozenset(kinds))

  @classmethod
  def where(cls, attribute: str, value: Any) -> 'RecordFilter':
    return cls(predicates=(AttributePredicate(attribute, value),))

  def and_where(self, attribute: str, value: Any) -> 'RecordFilter':
    return replace(self, predicates=(*self.predicates, AttributePredicate(attribute, value)))

  def includes(self, record: DisplayRecord) -> bool:
    if self.included_kinds is not None and record.kind not in self.included_kinds:
      return False
    if record.kind in self.excluded_kinds:
      return False
    return all(predicate.matches(record) for predicate in self.predicates)


@dataclass(frozen=True)
class ContentLimits:
  compact: int | None = 240
  normal: int | None = 2000
  full: int | None = None
  debug: int | None = None
  kind_overrides: tuple[tuple[RecordKind, int | None], ...] = ()

  def __post_init__(self) -> None:
    values = [getattr(self, name) for name in ('compact', 'normal', 'full', 'debug')]
    values.extend(limit for _, limit in self.kind_overrides)
    if any(value is not None and value <= 0 for value in values):
      raise ValueError('content limits must be positive or None')
    kinds = [kind for kind, _ in self.kind_overrides]
    if len(kinds) != len(set(kinds)):
      raise ValueError('content limit overrides contain duplicate record kinds')

  def for_record(self, kind: RecordKind, verbosity: Verbosity) -> int | None:
    for candidate, limit in self.kind_overrides:
      if candidate is kind:
        return limit
    return getattr(self, verbosity.value)


@dataclass(frozen=True)
class OutputRoutes:
  conversation: OutputRoute = OutputRoute.CONVERSATION
  reply: OutputRoute = OutputRoute.REPLY
  trace: OutputRoute = OutputRoute.TRACE
  metadata: OutputRoute = OutputRoute.METADATA
  status: OutputRoute = OutputRoute.STATUS


_DEFAULT_LABEL_PAIRS = (
  (RecordKind.SYSTEM_PROMPT, 'system'),
  (RecordKind.USER_INPUT, 'user'),
  (RecordKind.REASONING, 'reasoning'),
  (RecordKind.INTERIM_ASSISTANT, 'assistant'),
  (RecordKind.ASSISTANT, 'reply'),
  (RecordKind.LLM_CALL, 'llm call'),
  (RecordKind.TOOL_CALL, 'tool call'),
  (RecordKind.TOOL_RESULT, 'tool result'),
  (RecordKind.ERROR, 'error'),
  (RecordKind.HARNESS_EVENT, 'harness event'),
  (RecordKind.TRAIL_METADATA, 'trail'),
  (RecordKind.LAUNCH_CONTEXT, 'session context'),
  (RecordKind.SEGMENT_BOUNDARY, 'resumed'),
  (RecordKind.NATIVE_STEP, 'step'),
  (RecordKind.TRAIL_LIST_ROW, 'trail'),
  (RecordKind.LINEAGE_NODE, 'trail'),
  (RecordKind.NOTICE, 'notice'),
  (RecordKind.TRANSIENT_ACTIVITY, 'status'),
)


@dataclass(frozen=True)
class Labels:
  values: tuple[tuple[RecordKind, str], ...] = _DEFAULT_LABEL_PAIRS

  def __post_init__(self) -> None:
    keys = [kind for kind, _ in self.values]
    if len(keys) != len(set(keys)):
      raise ValueError('display labels contain duplicate record kinds')
    missing = ALL_RECORD_KINDS - frozenset(keys)
    if len(missing) > 0:
      raise ValueError(f'display labels are missing {sorted(missing)}')
    if any(len(label) == 0 for _, label in self.values):
      raise ValueError('display labels must not be empty')
    if any(
      any(ord(character) < 32 or ord(character) == 127 for character in label)
      for _, label in self.values
    ):
      raise ValueError('display labels cannot contain control characters')

  def for_kind(self, kind: RecordKind) -> str:
    for candidate, label in self.values:
      if candidate is kind:
        return label
    raise AssertionError(f'label validation missed {kind}')

  def override(self, *values: tuple[RecordKind, str]) -> 'Labels':
    replacements = dict(values)
    return Labels(tuple((kind, replacements.get(kind, label)) for kind, label in self.values))


@dataclass(frozen=True)
class DisplayConfig:
  record_filter: RecordFilter = RecordFilter()
  verbosity: Verbosity = Verbosity.NORMAL
  detail_overrides: tuple[tuple[RecordKind, Verbosity], ...] = ()
  layout: Layout = Layout.EVENT_LOG
  appearance: Appearance = Appearance.PLAIN_LOG
  color: ColorMode = ColorMode.AUTO
  content_limits: ContentLimits = ContentLimits()
  hidden_content_kinds: frozenset[RecordKind] = frozenset()
  timestamps: TimestampPolicy = TimestampPolicy.WHEN_KNOWN
  routes: OutputRoutes = OutputRoutes()
  labels: Labels = Labels()
  context_label: str = ''
  paging: bool = False

  def __post_init__(self) -> None:
    kinds = [kind for kind, _ in self.detail_overrides]
    if len(kinds) != len(set(kinds)):
      raise ValueError('detail overrides contain duplicate record kinds')
    if any(ord(character) < 32 or ord(character) == 127 for character in self.context_label):
      raise ValueError('display context label cannot contain control characters')
    if self.appearance is Appearance.CHAT and self.layout is not Layout.CONVERSATION:
      raise ValueError('chat appearance requires conversation layout')
    if self.appearance is Appearance.REWIND and self.layout is Layout.EVENT_LOG:
      raise ValueError('rewind appearance requires a rewind layout')
    if self.layout is Layout.NATIVE_STEPS and self.record_filter.included_kinds is not None:
      if RecordKind.NATIVE_STEP not in self.record_filter.included_kinds:
        raise ValueError('native-steps layout must allow native step records')
    if self.layout is Layout.TRAIL_LIST and self.record_filter.included_kinds is not None:
      if RecordKind.TRAIL_LIST_ROW not in self.record_filter.included_kinds:
        raise ValueError('trail-list layout must allow trail list rows')
    if self.layout is Layout.LINEAGE_TREE and self.record_filter.included_kinds is not None:
      if RecordKind.LINEAGE_NODE not in self.record_filter.included_kinds:
        raise ValueError('lineage-tree layout must allow lineage nodes')

  def detail_for(self, kind: RecordKind) -> Verbosity:
    for candidate, verbosity in self.detail_overrides:
      if candidate is kind:
        return verbosity
    return self.verbosity

  def override(self, **changes: Any) -> 'DisplayConfig':
    return replace(self, **changes)


class PresetName(StrEnum):
  OBSERVER = 'observer'
  ASK = 'ask'
  CALL = 'call'
  CHAT = 'chat'
  REWIND_SHOW = 'rewind-show'
  REWIND_STEPS = 'rewind-steps'
  REWIND_LIST = 'rewind-list'
  REWIND_TREE = 'rewind-tree'
  REWIND_GREP = 'rewind-grep'


def _filter_for(kinds: frozenset[RecordKind], *excluded: RecordKind) -> RecordFilter:
  return RecordFilter(included_kinds=kinds - frozenset(excluded))


def _build_presets() -> Mapping[PresetName, DisplayConfig]:
  activity_kinds = CONVERSATION_RECORD_KINDS | {
    RecordKind.NOTICE,
    RecordKind.TRANSIENT_ACTIVITY,
  }
  all_trace = OutputRoutes(
    conversation=OutputRoute.TRACE,
    reply=OutputRoute.TRACE,
    trace=OutputRoute.TRACE,
    metadata=OutputRoute.TRACE,
    status=OutputRoute.TRACE,
  )
  chat_labels = Labels().override(
    (RecordKind.USER_INPUT, 'you'),
    (RecordKind.REASONING, 'thinking'),
  )
  rewind_labels = Labels().override(
    (RecordKind.USER_INPUT, 'USER'),
    (RecordKind.REASONING, 'thinking'),
    (RecordKind.INTERIM_ASSISTANT, 'ASSISTANT'),
    (RecordKind.ASSISTANT, 'ASSISTANT'),
    (RecordKind.TOOL_CALL, 'ASSISTANT'),
    (RecordKind.TOOL_RESULT, 'ASSISTANT'),
    (RecordKind.ERROR, 'ERROR'),
    (RecordKind.LAUNCH_CONTEXT, 'SESSION CONTEXT'),
  )
  log_limits = ContentLimits(
    normal=4000,
    kind_overrides=(
      (RecordKind.TOOL_CALL, 1500),
      (RecordKind.TOOL_RESULT, 1500),
    ),
  )
  observer = DisplayConfig(
    record_filter=_filter_for(activity_kinds),
    layout=Layout.EVENT_LOG,
    appearance=Appearance.PLAIN_LOG,
    color=ColorMode.NEVER,
    content_limits=log_limits,
    routes=all_trace,
  )
  ask = DisplayConfig(
    record_filter=_filter_for(activity_kinds, RecordKind.USER_INPUT),
    layout=Layout.EVENT_LOG,
    appearance=Appearance.PLAIN_LOG,
    color=ColorMode.NEVER,
    content_limits=log_limits,
    routes=OutputRoutes(
      conversation=OutputRoute.TRACE,
      reply=OutputRoute.REPLY,
      trace=OutputRoute.TRACE,
      metadata=OutputRoute.TRACE,
      status=OutputRoute.TRACE,
    ),
  )
  conversation = DisplayConfig(
    record_filter=_filter_for(
      activity_kinds,
      RecordKind.SYSTEM_PROMPT,
      RecordKind.LLM_CALL,
      RecordKind.HARNESS_EVENT,
    ),
    verbosity=Verbosity.COMPACT,
    detail_overrides=(
      (RecordKind.USER_INPUT, Verbosity.FULL),
      (RecordKind.INTERIM_ASSISTANT, Verbosity.FULL),
      (RecordKind.ASSISTANT, Verbosity.FULL),
    ),
    layout=Layout.CONVERSATION,
    appearance=Appearance.CHAT,
    color=ColorMode.NEVER,
    hidden_content_kinds=frozenset({RecordKind.TOOL_RESULT}),
    timestamps=TimestampPolicy.WHEN_KNOWN,
    labels=chat_labels,
  )
  rewind_show_kinds = CONVERSATION_RECORD_KINDS - {
    RecordKind.SYSTEM_PROMPT,
    RecordKind.LLM_CALL,
    RecordKind.HARNESS_EVENT,
  } | {
    RecordKind.TRAIL_METADATA,
    RecordKind.LAUNCH_CONTEXT,
    RecordKind.SEGMENT_BOUNDARY,
  }
  rewind_show = DisplayConfig(
    record_filter=_filter_for(rewind_show_kinds),
    verbosity=Verbosity.FULL,
    layout=Layout.CONVERSATION,
    appearance=Appearance.REWIND,
    timestamps=TimestampPolicy.PLACEHOLDER,
    labels=rewind_labels,
    paging=True,
  )
  return MappingProxyType(
    {
      PresetName.OBSERVER: observer,
      PresetName.ASK: ask,
      PresetName.CALL: conversation,
      PresetName.CHAT: conversation,
      PresetName.REWIND_SHOW: rewind_show,
      PresetName.REWIND_STEPS: DisplayConfig(
        record_filter=_filter_for(frozenset({RecordKind.TRAIL_METADATA, RecordKind.NATIVE_STEP})),
        verbosity=Verbosity.FULL,
        layout=Layout.NATIVE_STEPS,
        appearance=Appearance.REWIND,
        timestamps=TimestampPolicy.PLACEHOLDER,
        paging=True,
      ),
      PresetName.REWIND_LIST: DisplayConfig(
        record_filter=_filter_for(frozenset({RecordKind.TRAIL_LIST_ROW})),
        verbosity=Verbosity.COMPACT,
        layout=Layout.TRAIL_LIST,
        appearance=Appearance.REWIND,
        timestamps=TimestampPolicy.PLACEHOLDER,
        paging=True,
      ),
      PresetName.REWIND_TREE: DisplayConfig(
        record_filter=_filter_for(frozenset({RecordKind.LINEAGE_NODE})),
        verbosity=Verbosity.COMPACT,
        layout=Layout.LINEAGE_TREE,
        appearance=Appearance.REWIND,
        timestamps=TimestampPolicy.HIDDEN,
      ),
      PresetName.REWIND_GREP: rewind_show.override(
        color=ColorMode.NEVER,
        paging=False,
      ),
    }
  )


PRESETS = _build_presets()


def preset(name: PresetName | str, **overrides: Any) -> DisplayConfig:
  try:
    preset_name = PresetName(name)
  except ValueError as exception:
    raise KeyError(f'unknown display preset: {name}') from exception
  configuration = PRESETS[preset_name]
  return configuration if len(overrides) == 0 else configuration.override(**overrides)


assert STRUCTURAL_RECORD_KINDS <= ALL_RECORD_KINDS
