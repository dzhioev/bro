import pytest

from bro.llm.tracker import NullTracker, Tracker


class TestNullTracker:
  def test_start_returns_nothing(self):
    tracker = NullTracker()
    result = tracker.start(
      bro='b', recipe={}, system_prompt='', forked_from=None, interactive=False, surface='x'
    )
    assert result is None

  def test_methods_are_noops(self):
    tracker = NullTracker()
    tracker.start(
      bro='b', recipe={}, system_prompt='p', forked_from=None, interactive=True, surface='x'
    )
    assert tracker.step('user_input', 'hello', turn_index=0) is None
    tracker.step('llm_call', {'request': {}, 'response': {}}, call_index=1, turn_index=0)
    tracker.end('ok')
    assert tracker.current_tool_step_id is None


class TestTrackerIsABC:
  def test_cannot_instantiate_base_class(self):
    with pytest.raises(TypeError):
      Tracker()  # type: ignore[abstract]
