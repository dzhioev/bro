import pytest

from llm.tracker import NullTracker, Tracker


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
    assert tracker.step('reasoning', 'r', turn_index=1) is None
    tracker.step('end', {'reason': 'ok'})
    tracker.end_trail('ok')


class TestTrackerIsABC:
  def test_cannot_instantiate_base_class(self):
    with pytest.raises(TypeError):
      Tracker()  # type: ignore[abstract]
