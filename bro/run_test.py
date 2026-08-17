from unittest.mock import patch

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
