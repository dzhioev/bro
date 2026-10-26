from unittest.mock import patch

import pytest

from bro.base import configs
from bro.base.source_commit import SourceCommit
from bro.run import main


def test_run_dispatches_to_the_in_process_launcher():
  with patch('bro.launch.run.run_main', return_value=0) as run:
    assert main(['bro', 'run', 'dev', 'hello']) == 0
  run.assert_called_once_with(['bro', 'dev', 'hello'], program=['bro', 'run'])


def test_global_flag_before_run_reaches_the_launcher():
  with patch('bro.launch.run.run_main', return_value=0) as run:
    assert main(['bro', '--verbose', 'run', 'dev', 'hello']) == 0
  run.assert_called_once_with(['bro', '--verbose', 'dev', 'hello'], program=['bro', 'run'])


def test_chat_dispatches_to_the_in_process_launcher():
  with patch('bro.launch.call.chat_main', return_value=0) as chat:
    assert main(['bro', 'chat', 'dev', 'hello']) == 0
  chat.assert_called_once_with(['bro', 'dev', 'hello'], program=['bro', 'chat'])


@pytest.mark.parametrize(
  ('commit', 'suffix'),
  [
    (SourceCommit('c0ffee', modified=False), ' (c0ffee)'),
    (SourceCommit('c0ffee', modified=True), ' (c0ffee, modified)'),
    (None, ''),
  ],
  ids=['clean', 'modified', 'unrecorded'],
)
def test_version_names_the_distribution_and_its_commit(capsys, commit, suffix):
  with patch('bro.base.source_commit.installed_commit', return_value=commit):
    with pytest.raises(SystemExit) as exited:
      main(['bro', '--version'])

  assert exited.value.code == 0
  assert capsys.readouterr().out == f'{configs.DISTRIBUTION} {configs.VERSION}{suffix}\n'
