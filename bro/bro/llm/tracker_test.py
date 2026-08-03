import pytest

from bro.llm.tracker import NullTracker, Tracker


class TestNullTracker:
  def test_start_trail_returns_empty_string(self):
    tracker = NullTracker()
    trail_id = tracker.start_trail(
      bro='b', llm_spec={}, system_prompt='', forked_from=None, interactive=False, surface='x'
    )
    assert trail_id == ''

  def test_methods_are_noops(self):
    tracker = NullTracker()
    tracker.start_trail(
      bro='b', llm_spec={}, system_prompt='p', forked_from=None, interactive=True, surface='x'
    )
    assert tracker.step('user_input', 'hello', turn_index=0) is None
    tracker.step('llm_call', {'request': {}, 'response': {}}, call_index=1, turn_index=0)
    tracker.end_trail('ok')
    assert tracker.current_tool_step_id is None


class TestTrackerIsABC:
  def test_cannot_instantiate_base_class(self):
    with pytest.raises(TypeError):
      Tracker()  # type: ignore[abstract]
