from unittest.mock import Mock

import pytest

from bro.launch.resume import conversation_history, resume
from bro.llm.tracker import RunFork
from bro.native.run_log import latest, new_path, read
from bro.native.run_log_test_helper import conversation_log as log, echo_call as call
from bro.trails.display import AssistantText, UserInput


def test_history_follows_chain_and_honors_the_selected_bound(tmp_path):
  root = log(tmp_path / 'root.jsonl')
  root.step('user_input', 'hello')
  call(root, text='first reply')
  root.step('user_input', 'excluded root input')
  middle = log(tmp_path / 'middle.jsonl', parent=RunFork('root', 2))
  middle.step('user_input', 'middle')
  call(middle, text='middle reply')
  child = log(tmp_path / 'child.jsonl', parent=RunFork('middle', 2))
  child.step('user_input', 'child')
  call(child, text='excluded child reply')
  records = conversation_history(read(child.path), 1)
  assert [item.content for item in records if isinstance(item, (UserInput, AssistantText))] == [
    'hello',
    'first reply',
    'middle',
    'middle reply',
    'child',
  ]


def test_resume_checks_the_bro_before_forking(tmp_path, monkeypatch):
  writer = log(tmp_path / 'run.jsonl')
  writer.step('user_input', 'hello')
  monkeypatch.setattr('bro.launch.resume.fork', Mock(side_effect=AssertionError('forked')))
  with pytest.raises(ValueError, match='belongs to bro'):
    resume('another', str(writer.path))


def test_latest_selects_the_newest_log_of_any_surface():
  directory = new_path().parent
  other = log(directory / '002.jsonl')
  other.step('user_input', 'latest')
  log(directory / '001.jsonl')
  assert latest('bro') == other.path
  assert latest('absent') is None


def test_resume_latest_continues_a_local_log_without_a_store(monkeypatch):
  path = new_path()
  writer = log(path)
  writer.step('user_input', 'hello')
  monkeypatch.setattr(
    'bro.trails.store.default_store', Mock(side_effect=AssertionError('store read'))
  )
  result = resume('bro', 'latest')
  assert result.run == path
  assert result.runner.run_log is not None
  assert result.runner.run_log.parent == path.parent
  result.runner.end()


def test_latest_does_not_scan_past_two_hundred_files():
  from bro.native.run_log import RunLog

  directory = new_path().parent
  for index in range(201):
    writer = RunLog(directory / f'{index:03}.jsonl')
    writer.start('bro' if index == 0 else 'another', {}, 'prompt', None, False, 'test')
  assert latest('bro') is None
