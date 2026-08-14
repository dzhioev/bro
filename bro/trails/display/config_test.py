from dataclasses import FrozenInstanceError

import pytest

from bro.trails.display import (
  ALL_RECORD_KINDS,
  CONVERSATION_RECORD_KINDS,
  PRESETS,
  AssistantText,
  ColorMode,
  ContentLimits,
  DisplayConfig,
  Layout,
  LiveSource,
  Origin,
  OutputRoute,
  PresetName,
  RecordedSource,
  RecordFilter,
  RecordKind,
  TimestampPolicy,
  ToolResult,
  Verbosity,
  preset,
)


def _reply() -> AssistantText:
  return AssistantText(
    key='reply',
    origin=Origin.LIVE,
    source=LiveSource('run', 1),
    content='done',
  )


class TestRecordProvenance:
  def test_recorded_and_live_records_allow_unknown_sources_but_reject_mismatches(self):
    record = AssistantText(key='x', origin=Origin.RECORDED, content='unknown source')
    assert record.source is None
    with pytest.raises(ValueError, match='LiveSource'):
      AssistantText(
        key='mismatch',
        origin=Origin.LIVE,
        source=RecordedSource('trail', 1),
        content='bad',
      )

  def test_surface_records_cannot_claim_durable_provenance(self):
    with pytest.raises(ValueError, match='cannot claim'):
      AssistantText(
        key='x',
        origin=Origin.SURFACE,
        source=LiveSource('run', 1),
        content='bad',
      )

  def test_records_are_frozen(self):
    record = _reply()
    with pytest.raises(FrozenInstanceError):
      record.content = 'changed'  # type: ignore[misc]


class TestRecordFilter:
  def test_include_exclude_and_attribute_predicates(self):
    reply = _reply()
    assert RecordFilter.including(RecordKind.ASSISTANT).includes(reply)
    assert not RecordFilter.excluding(RecordKind.ASSISTANT).includes(reply)
    assert RecordFilter.where('source.run_id', 'run').includes(reply)
    assert not RecordFilter.where('source.run_id', 'other').includes(reply)

  def test_rejects_overlapping_visibility_rules(self):
    with pytest.raises(ValueError, match='includes and excludes'):
      RecordFilter(
        included_kinds=frozenset({RecordKind.ASSISTANT}),
        excluded_kinds=frozenset({RecordKind.ASSISTANT}),
      )


class TestConfiguration:
  def test_detail_override_and_copy_are_validated(self):
    configuration = DisplayConfig(
      verbosity=Verbosity.COMPACT,
      detail_overrides=((RecordKind.ASSISTANT, Verbosity.FULL),),
    )
    assert configuration.detail_for(RecordKind.REASONING) is Verbosity.COMPACT
    assert configuration.detail_for(RecordKind.ASSISTANT) is Verbosity.FULL
    assert configuration.override(color=ColorMode.NEVER).color is ColorMode.NEVER

  def test_content_limits_reject_nonpositive_values(self):
    with pytest.raises(ValueError, match='positive'):
      ContentLimits(normal=0)

  def test_layout_rejects_a_filter_that_cannot_supply_its_records(self):
    with pytest.raises(ValueError, match='native step'):
      DisplayConfig(
        layout=Layout.NATIVE_STEPS,
        record_filter=RecordFilter.including(RecordKind.ASSISTANT),
      )


class TestPresets:
  def test_registry_contains_every_named_preset(self):
    assert set(PRESETS) == set(PresetName)
    assert all(configuration.labels.values for configuration in PRESETS.values())
    assert all(
      {kind for kind, _ in configuration.labels.values} == ALL_RECORD_KINDS
      for configuration in PRESETS.values()
    )

  @pytest.mark.parametrize(
    ('name', 'layout', 'verbosity', 'visible_kinds'),
    [
      (
        PresetName.OBSERVER,
        Layout.EVENT_LOG,
        Verbosity.NORMAL,
        CONVERSATION_RECORD_KINDS | {RecordKind.NOTICE, RecordKind.TRANSIENT_ACTIVITY},
      ),
      (
        PresetName.ASK,
        Layout.EVENT_LOG,
        Verbosity.NORMAL,
        (CONVERSATION_RECORD_KINDS | {RecordKind.NOTICE, RecordKind.TRANSIENT_ACTIVITY})
        - {RecordKind.USER_INPUT},
      ),
      (
        PresetName.CALL,
        Layout.CONVERSATION,
        Verbosity.COMPACT,
        (CONVERSATION_RECORD_KINDS | {RecordKind.NOTICE, RecordKind.TRANSIENT_ACTIVITY})
        - {
          RecordKind.SYSTEM_PROMPT,
          RecordKind.LLM_CALL,
          RecordKind.TOOL_RESULT,
          RecordKind.HARNESS_EVENT,
        },
      ),
      (
        PresetName.CHAT,
        Layout.CONVERSATION,
        Verbosity.COMPACT,
        (CONVERSATION_RECORD_KINDS | {RecordKind.NOTICE, RecordKind.TRANSIENT_ACTIVITY})
        - {
          RecordKind.SYSTEM_PROMPT,
          RecordKind.LLM_CALL,
          RecordKind.TOOL_RESULT,
          RecordKind.HARNESS_EVENT,
        },
      ),
      (
        PresetName.REWIND_SHOW,
        Layout.CONVERSATION,
        Verbosity.FULL,
        CONVERSATION_RECORD_KINDS
        | {
          RecordKind.TRAIL_METADATA,
          RecordKind.LAUNCH_CONTEXT,
          RecordKind.SEGMENT_BOUNDARY,
        },
      ),
      (
        PresetName.REWIND_STEPS,
        Layout.NATIVE_STEPS,
        Verbosity.FULL,
        {RecordKind.TRAIL_METADATA, RecordKind.NATIVE_STEP},
      ),
      (
        PresetName.REWIND_LIST,
        Layout.TRAIL_LIST,
        Verbosity.COMPACT,
        {RecordKind.TRAIL_LIST_ROW},
      ),
      (
        PresetName.REWIND_TREE,
        Layout.LINEAGE_TREE,
        Verbosity.COMPACT,
        {RecordKind.LINEAGE_NODE},
      ),
      (
        PresetName.REWIND_GREP,
        Layout.CONVERSATION,
        Verbosity.FULL,
        CONVERSATION_RECORD_KINDS
        | {
          RecordKind.TRAIL_METADATA,
          RecordKind.LAUNCH_CONTEXT,
          RecordKind.SEGMENT_BOUNDARY,
        },
      ),
    ],
  )
  def test_each_preset_has_a_complete_scenario_contract(
    self,
    name: PresetName,
    layout: Layout,
    verbosity: Verbosity,
    visible_kinds: set[RecordKind] | frozenset[RecordKind],
  ):
    configuration = preset(name)
    assert configuration.layout is layout
    assert configuration.verbosity is verbosity
    assert configuration.record_filter.included_kinds == frozenset(visible_kinds)

  def test_ask_routes_exactly_the_terminal_reply_to_reply(self):
    ask = preset('ask')
    assert ask.routes.reply is OutputRoute.REPLY
    assert ask.routes.trace is OutputRoute.TRACE
    assert ask.routes.conversation is OutputRoute.TRACE

  def test_call_and_chat_hide_result_bodies_but_remain_separate_names(self):
    result = ToolResult(
      key='result',
      origin=Origin.LIVE,
      source=LiveSource('run', 2),
      call_id='call',
      result='secret',
    )
    assert not preset('call').record_filter.includes(result)
    assert not preset('chat').record_filter.includes(result)
    assert PresetName.CALL is not PresetName.CHAT

  def test_rewind_grep_is_plain_unpaged_show(self):
    show = preset('rewind-show')
    grep = preset('rewind-grep')
    assert grep.record_filter == show.record_filter
    assert grep.verbosity is show.verbosity
    assert grep.color is ColorMode.NEVER
    assert not grep.paging
    assert show.paging

  def test_preset_overrides_use_configuration_validation(self):
    overridden = preset(
      'observer',
      color=ColorMode.ALWAYS,
      timestamps=TimestampPolicy.HIDDEN,
    )
    assert overridden.color is ColorMode.ALWAYS
    assert overridden.timestamps is TimestampPolicy.HIDDEN
    with pytest.raises(KeyError, match='unknown display preset'):
      preset('missing')
