import os
from typing import Optional
from unittest.mock import patch

import pytest

from bro.launch.do_task import fix_invocation, main


def test_wraps_raw_ref_as_fix_invocation():
  assert fix_invocation('abc-123') == '/fix abc-123'


def test_wraps_url_as_fix_invocation():
  url = 'https://www.notion.so/foo/add-X-369d38d85a6d818caf91c12a203b17e1?source=copy_link'
  assert fix_invocation(url) == f'/fix {url}'


def test_passes_through_leading_slash_invocation():
  # `do-task ppp-dev "/fix --new idea"` must not become `/fix /fix --new idea`,
  # and an alternate skill (`/pr …`) is a deliberate override left untouched.
  assert fix_invocation('/fix --new idea') == '/fix --new idea'
  assert fix_invocation('/pr') == '/pr'


def test_leading_whitespace_is_wrapped_not_passed_through():
  # the pass-through rule keys on the first character, so a leading-whitespace
  # input is wrapped like any other ref rather than treated as an invocation.
  assert fix_invocation('  /pr') == '/fix   /pr'


def test_main_re_execs_into_container_when_outside():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container', return_value=0) as run,
  ):
    env.pop('CW_IN_CONTAINER', None)
    env.pop('PPP_SHELL_COMMAND', None)
    rc = main(['do-task', 'ppp-dev', 'abc-123'])
    assert rc == 0
    assert env['PPP_SHELL_COMMAND'] == 'do-task ppp-dev abc-123'
    launch = run.call_args.args[0]
    assert launch.name.startswith('do-task-ppp-dev-')
    assert launch.command == ['bro', 'run', 'ppp-dev', '/fix abc-123', '--in-place']
    assert run.call_args.kwargs['drop'] is True
    assert {'github', 'brog', 'trails'} <= launch.secrets
    assert launch.docker_sock is False


def test_main_summon_wraps_the_task_as_a_fix_invocation():
  with (
    patch.dict('os.environ', {'CW_IN_CONTAINER': '1', 'BROKER_CHANNEL': 'unix:/tmp/x.sock'}),
    patch('summon.relay_summon', return_value=0) as relay,
  ):
    rc = main(['do-task', 'ppp-dev', 'abc-123', '--summon'])
  assert rc == 0
  relay.assert_called_once_with('ppp-dev', '/fix abc-123', timeout=None, into=None)


def test_main_exports_task_id_before_the_hop():
  # the hop forwards CW_TASK_ID from this process's environment at container
  # create time, so the export must precede run_in_container.
  seen_at_hop: dict[str, Optional[str]] = {}

  def record_env(*args, **kwargs):
    seen_at_hop['task_id'] = os.environ.get('CW_TASK_ID')
    return 0

  url = 'https://www.notion.so/foo/add-X-369d38d85a6d818caf91c12a203b17e1?source=copy_link'
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container', side_effect=record_env),
  ):
    env.pop('CW_IN_CONTAINER', None)
    env.pop('CW_TASK_ID', None)
    rc = main(['do-task', 'ppp-dev', url])
  assert rc == 0
  assert seen_at_hop['task_id'] == '369d38d8-5a6d-818c-af91-c12a203b17e1'


def test_main_exports_task_id_for_dashed_uuid():
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container', return_value=0),
  ):
    env.pop('CW_IN_CONTAINER', None)
    env.pop('CW_TASK_ID', None)
    rc = main(['do-task', 'ppp-dev', '369d38d8-5a6d-818c-af91-c12a203b17e1'])
    assert rc == 0
    assert os.environ.get('CW_TASK_ID') == '369d38d8-5a6d-818c-af91-c12a203b17e1'


@pytest.mark.parametrize('task', ['fix the frobnicator', '/fix --new idea'])
def test_main_exports_nothing_for_non_page_ref_input(task: str):
  # a description or slash invocation names no page — the bro resolves the task
  # itself, and any ambient CW_TASK_ID is left as-is.
  with (
    patch.dict('os.environ', {}, clear=False) as env,
    patch('cw.run_in_container', return_value=0),
  ):
    env.pop('CW_IN_CONTAINER', None)
    env.pop('CW_TASK_ID', None)
    rc = main(['do-task', 'ppp-dev', task])
    assert rc == 0
    assert os.environ.get('CW_TASK_ID') is None
