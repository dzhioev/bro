import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from bro.launch.run import run_main


@pytest.fixture
def in_process_run(monkeypatch):
  bro = MagicMock(name='bro')
  bro.name = 'dev'
  bro.run = AsyncMock(return_value='done')
  monkeypatch.setattr('bro.launch.run.create_bro_for_run', lambda name, selection: bro)
  monkeypatch.setattr('bro.launch.run._ask_observer', lambda name, rich: MagicMock())
  monkeypatch.setattr('bro.launch.broxy.session_broxy', contextlib.nullcontext)
  return bro


def test_runs_in_the_calling_process(in_process_run):
  assert run_main(['bro', 'dev', 'hello'], program=['bro', 'run']) is None
  in_process_run.run.assert_awaited_once()
  assert in_process_run.run.call_args.args == ('hello',)
  assert in_process_run.run.call_args.kwargs['surface'] == 'ask'
  assert in_process_run.run.call_args.kwargs['hold'] == 'unattended'


def test_in_place_is_a_suppressed_no_op(in_process_run):
  assert run_main(['bro', 'dev', 'hello', '--in-place'], program=['bro', 'run']) is None
  in_process_run.run.assert_awaited_once()


@pytest.mark.parametrize(
  'flag',
  ['--summon', '--grant', '--revoke', '--into', '--no-trails', '--timeout', '--detach'],
)
def test_runtime_flags_are_not_accepted(flag):
  with pytest.raises(SystemExit):
    run_main(['bro', 'dev', 'hello', flag], program=['bro', 'run'])
