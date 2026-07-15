#!/usr/bin/env python
import os
import re
import shlex
import types
from datetime import UTC
from typing import cast

import pytest

import brog.system
import cw
import cw.paths
import dive_in

UUID = '35ad38d8-5a6d-81ea-bce6-e4caf17ece7f'
HEX = '35ad38d85a6d81eabce6e4caf17ece7f'
URL = f'https://app.notion.com/p/my-task-{HEX}'


def _brog_task(name: str = 'my task'):
  from brog.model import Task

  return Task(
    id=UUID,
    name=name,
    status='open',
    url=URL,
    tags=[],
    project=None,
    blocked_by=[],
  )


@pytest.fixture
def fake_proj(monkeypatch, tmp_path):
  monkeypatch.setattr(cw.paths, '_project_root', lambda: tmp_path)
  worktrees = tmp_path / 'var' / 'cw' / 'worktrees'
  containers = tmp_path / 'var' / 'cw' / 'containers'
  worktrees.mkdir(parents=True)
  containers.mkdir(parents=True)
  return worktrees, containers


class TestLaunchCommand:
  """the emitted `cw ss ...` command must parse cleanly under cw's own ss parser."""

  @pytest.mark.parametrize(
    'kwargs',
    [
      {},
      {'forwarded': ['--host']},
      {'forwarded': ['--host', '--mode', 'guided']},
      {'command': 'do a thing', 'new': True},
    ],
  )
  def test_emitted_command_parses(self, kwargs, fake_proj, capsys):
    forwarded = kwargs.pop('forwarded', [])
    rc = dive_in.dive_in(forwarded=forwarded, dry_run=True, **kwargs)
    assert rc == 0
    tokens = shlex.split(capsys.readouterr().out.strip())
    assert tokens[0] == 'cw'
    # Parser.parse strips argv[0] as the program name, mirroring cw.main(['cw', ...])
    args = cw.build_parser().parse(tokens)
    assert args['cmd'] == 'ss'
    assert len(args['name']) > 0
    assert len(args['claude_args']) == 0  # nothing leaked into the forwarded REMAINDER

  def test_defaults_to_attended(self, fake_proj, capsys):
    rc = dive_in.main(['dive-in', '-n'])
    assert rc == 0
    tokens = shlex.split(capsys.readouterr().out.strip())
    assert '--mode' not in tokens
    args = cw.build_parser().parse(tokens)
    assert args['mode'] == 'attended'

  def test_forwarded_flags_ride_verbatim(self, fake_proj, capsys, monkeypatch):
    monkeypatch.delenv('CW_BRO', raising=False)
    rc = dive_in.dive_in(forwarded=['--bro', 'ppp-dev'], dry_run=True)
    assert rc == 0
    args = cw.build_parser().parse(shlex.split(capsys.readouterr().out.strip()))
    assert args['bro'] == 'ppp-dev'
    assert len(args['claude_args']) == 0
    # cw owns the session theming (persona default, CW_BRO export); dive-in
    # must not preempt it
    assert 'CW_BRO' not in os.environ

  def test_persona_rides_the_forwarded_flags(self, fake_proj, capsys, monkeypatch):
    monkeypatch.delenv('CW_BRO', raising=False)
    rc = dive_in.dive_in(forwarded=['--persona', 'pm'], dry_run=True)
    assert rc == 0
    args = cw.build_parser().parse(shlex.split(capsys.readouterr().out.strip()))
    assert args['persona'] == 'pm'
    assert 'CW_BRO' not in os.environ


class TestShellCommandReconstruction:
  """the visual banner receives the wrapper invocation, not the underlying `cw ss`."""

  def test_forwarded_flags_appear_in_the_reconstruction(self, fake_proj, monkeypatch):
    monkeypatch.delenv('PPP_SHELL_COMMAND', raising=False)
    rc = dive_in.main(['dive-in', '-n', '--mode', 'guided', '--bro', 'ppp-dev'])
    assert rc == 0
    assert os.environ['PPP_SHELL_COMMAND'] == 'dive-in --mode guided --bro ppp-dev'

  def test_new_seed_keeps_the_prompt_marker_tail(self, fake_proj, monkeypatch):
    monkeypatch.delenv('PPP_SHELL_COMMAND', raising=False)
    rc = dive_in.main(['dive-in', '-n', '--new', 'do a thing'])
    assert rc == 0
    # `cw banner` splits the user prompt off at the last ` --new ` marker
    assert os.environ['PPP_SHELL_COMMAND'] == 'dive-in --new do a thing'


class TestTaskMode:
  @pytest.fixture(autouse=True)
  def fake_backend(self, monkeypatch):
    monkeypatch.setattr('brog.system.default_system', lambda: object())

  def test_every_launch_picks_a_fresh_workspace_name(self, fake_proj, monkeypatch, capsys):
    monkeypatch.setattr(
      dive_in, '_prefetch_task', lambda system, ref: (_brog_task('My Task!'), 'task block')
    )
    rc = dive_in.dive_in(forwarded=[], dry_run=True, task=UUID)
    assert rc == 0
    name = shlex.split(capsys.readouterr().out.strip())[-1]
    assert re.fullmatch(r'my-task-[0-9a-f]{8}', name) is not None

  def test_empty_slug_falls_back_to_dive_in(self, fake_proj, monkeypatch, capsys):
    monkeypatch.setattr(
      dive_in, '_prefetch_task', lambda system, ref: (_brog_task('!!!'), 'task block')
    )
    rc = dive_in.dive_in(forwarded=[], dry_run=True, task=UUID)
    assert rc == 0
    name = shlex.split(capsys.readouterr().out.strip())[-1]
    assert re.fullmatch(r'dive-in-[0-9a-f]{8}', name) is not None

  def test_seeds_fix_with_the_original_ref_and_exports_the_canonical_id(
    self, fake_proj, monkeypatch, capsys
  ):
    monkeypatch.setattr(dive_in, '_prefetch_task', lambda system, ref: (_brog_task(), 'task block'))
    rc = dive_in.dive_in(forwarded=[], dry_run=True, task=URL)
    assert rc == 0
    tokens = shlex.split(capsys.readouterr().out.strip())
    prompt = tokens[tokens.index('-p') + 1]
    # the ref rides as typed; the prefetch block follows
    assert prompt.startswith(f'/fix {URL}\n\ntask block')
    # CW_TASK_ID carries the backend's canonical id, not the raw ref
    assert os.environ['CW_TASK_ID'] == UUID

  def test_focus_with_task_sets_focus_to_the_canonical_id(self, fake_proj, monkeypatch, capsys):
    focused = {}

    class FakeClient:
      def set_focus(self, task_id):
        focused['id'] = task_id

    monkeypatch.setattr(dive_in, '_is_flow_backend', lambda system: True)
    monkeypatch.setattr(dive_in, '_prefetch_task', lambda system, ref: (_brog_task(), 'task block'))
    monkeypatch.setattr(dive_in, 'default_client', lambda: FakeClient())
    rc = dive_in.dive_in(forwarded=[], dry_run=True, task=URL, focus=True)
    assert rc == 0
    assert focused['id'] == UUID
    # /fix has no focus form: the first message is the plain task-ref form
    tokens = shlex.split(capsys.readouterr().out.strip())
    prompt = tokens[tokens.index('-p') + 1]
    assert prompt.startswith(f'/fix {URL}')

  def test_bare_focus_seeds_fix_with_the_focused_id(self, fake_proj, monkeypatch, capsys):
    state = types.SimpleNamespace(task=types.SimpleNamespace(id=UUID))

    class FakeClient:
      def get_focus(self):
        return state

    monkeypatch.setattr(dive_in, '_is_flow_backend', lambda system: True)
    monkeypatch.setattr(dive_in, '_prefetch_task', lambda system, ref: (_brog_task(), 'task block'))
    monkeypatch.setattr(dive_in, 'default_client', lambda: FakeClient())
    rc = dive_in.dive_in(forwarded=[], dry_run=True, focus=True)
    assert rc == 0
    tokens = shlex.split(capsys.readouterr().out.strip())
    prompt = tokens[tokens.index('-p') + 1]
    assert prompt.startswith(f'/fix {UUID}')

  def test_focus_on_a_non_flow_backend_fails_at_launch(self, fake_proj):
    # the fake backend is a plain object — not the flow proxy — so --focus
    # must fail before any focus-client call
    rc = dive_in.dive_in(forwarded=[], dry_run=True, focus=True)
    assert rc == 1

  def test_new_and_focus_are_mutually_exclusive(self, fake_proj):
    with pytest.raises(SystemExit):
      dive_in.main(['dive-in', '-n', '--new', '--focus'])

  def test_resume_flag_is_rejected(self):
    with pytest.raises(SystemExit):
      dive_in.main(['dive-in', '--resume', '-t', UUID])


class TestPrefetchTask:
  def test_returns_task_and_embeds_metadata_description_and_comments(self):
    from datetime import datetime

    from brog.model import Comment, Project, Task

    task = Task(
      id=UUID,
      name='my task',
      status='open',
      url=URL,
      tags=['infra'],
      project=Project(id='project-1', name='proj', summary='the project'),
      blocked_by=[],
    )

    class FakeSystem:
      def get_task(self, task_ref):
        assert task_ref == URL
        return task

      def get_task_description(self, task_id):
        assert task_id == UUID
        return '## Goal\nDo the thing.'

      def get_task_comments(self, task_id):
        assert task_id == UUID
        return [
          Comment(
            topic='plan',
            author='ppp-dev',
            timestamp=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            body='the plan',
          )
        ]

    got, block = dive_in._prefetch_task(cast(brog.system.System, FakeSystem()), URL)
    assert got is task
    # description embedded verbatim
    assert '## Goal\nDo the thing.' in block
    # metadata embedded as json, nested project included
    assert '"status": "open"' in block
    assert '"name": "proj"' in block
    # comments embedded as json, datetimes stringified
    assert '"topic": "plan"' in block
    assert '2026-01-01 12:00:00+00:00' in block
    # instruction to skip the in-session fetch
    assert 'do not call get_task / read_task / read_comments' in block
