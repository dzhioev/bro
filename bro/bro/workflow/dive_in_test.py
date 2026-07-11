#!/usr/bin/env python
import os
import re
import shlex

import pytest

import cw
import dive_in
from dive_in import _pick_fresh_name

UUID = '35ad38d8-5a6d-81ea-bce6-e4caf17ece7f'
HEX = '35ad38d85a6d81eabce6e4caf17ece7f'


@pytest.fixture
def fake_proj(monkeypatch, tmp_path):
  monkeypatch.setattr(cw, '_project_root', lambda: tmp_path)
  worktrees = tmp_path / 'var' / 'cw' / 'worktrees'
  containers = tmp_path / 'var' / 'cw' / 'containers'
  worktrees.mkdir(parents=True)
  containers.mkdir(parents=True)
  return worktrees, containers


class TestPickFreshName:
  def test_appends_random_suffix(self, fake_proj):
    assert re.fullmatch(r'idea-[0-9a-f]{8}', _pick_fresh_name('idea')) is not None

  def test_regenerates_on_worktree_collision(self, fake_proj, monkeypatch):
    worktrees, _ = fake_proj
    suffixes = iter(['aaaaaa', 'bbbbbb'])
    monkeypatch.setattr(dive_in.secrets, 'token_hex', lambda _: next(suffixes))
    (worktrees / 'idea-aaaaaa').mkdir()
    assert _pick_fresh_name('idea') == 'idea-bbbbbb'

  def test_regenerates_on_container_collision(self, fake_proj, monkeypatch):
    _, containers = fake_proj
    suffixes = iter(['aaaaaa', 'bbbbbb'])
    monkeypatch.setattr(dive_in.secrets, 'token_hex', lambda _: next(suffixes))
    (containers / 'idea-aaaaaa').mkdir()
    assert _pick_fresh_name('idea') == 'idea-bbbbbb'


class TestLaunchCommand:
  """the emitted `cw ss ...` command must parse cleanly under cw's own ss parser.

  regression: dive-in passed a bare `--mcp` immediately before the positional name.
  `cw ss --mcp` is nargs='?' (const http), so with nothing between the flag and the
  name argparse consumed the name as --mcp's value and failed the choices check. A
  plain `dive-in` (no forwarded flags, no -p) is the path where nothing else sits
  between the flag and the name to mask the crash.
  """

  @pytest.mark.parametrize(
    'kwargs',
    [
      {},  # the path that crashed: nothing sits between --mcp and name
      {'forwarded': ['--host']},
      {'forwarded': ['--host', '--auto']},
      {'command': 'do a thing', 'new': True},
    ],
  )
  def test_emitted_command_parses_with_mcp_http(self, kwargs, fake_proj, capsys):
    forwarded = kwargs.pop('forwarded', [])
    rc = dive_in.dive_in(forwarded=forwarded, dry_run=True, **kwargs)
    assert rc == 0
    tokens = shlex.split(capsys.readouterr().out.strip())
    assert tokens[0] == 'cw'
    # Parser.parse strips argv[0] as the program name, mirroring cw.main(['cw', ...])
    args = cw.build_parser().parse(tokens)
    assert args['cmd'] == 'ss'
    assert args['mcp'] == 'http'
    assert len(args['name']) > 0
    assert len(args['claude_args']) == 0  # nothing leaked into the forwarded REMAINDER

  def test_bro_mode_forwards_bro_and_drops_mcp(self, fake_proj, capsys, monkeypatch):
    monkeypatch.delenv('CW_BRO', raising=False)
    rc = dive_in.dive_in(forwarded=['--bro', 'ppp-dev'], bro='ppp-dev', dry_run=True)
    assert rc == 0
    args = cw.build_parser().parse(shlex.split(capsys.readouterr().out.strip()))
    assert args['bro'] == 'ppp-dev'
    assert args['mcp'] is None
    assert len(args['claude_args']) == 0
    # the runner exports CW_BRO for a --bro session; dive-in must not preempt it
    assert 'CW_BRO' not in os.environ

  def test_native_mode_exports_default_cw_bro(self, fake_proj, monkeypatch):
    monkeypatch.delenv('CW_BRO', raising=False)
    rc = dive_in.dive_in(forwarded=[], dry_run=True)
    assert rc == 0
    assert os.environ['CW_BRO'] == 'ppp-dev'


class TestShellCommandReconstruction:
  """PPP_SHELL_COMMAND is rebuilt from dive-in's own parser (prog `dive-in`), so
  env-detection sees the wrapper invocation, not the underlying `cw ss`."""

  def test_forwarded_flags_appear_in_the_reconstruction(self, fake_proj, monkeypatch):
    monkeypatch.delenv('PPP_SHELL_COMMAND', raising=False)
    rc = dive_in.main(['dive-in', '-n', '--auto', '--bro', 'ppp-dev'])
    assert rc == 0
    assert os.environ['PPP_SHELL_COMMAND'] == 'dive-in --auto --bro ppp-dev'

  def test_new_seed_keeps_the_prompt_marker_tail(self, fake_proj, monkeypatch):
    monkeypatch.delenv('PPP_SHELL_COMMAND', raising=False)
    rc = dive_in.main(['dive-in', '-n', '--new', 'do a thing'])
    assert rc == 0
    # `cw banner` splits the user prompt off at the last ` --new ` marker
    assert os.environ['PPP_SHELL_COMMAND'] == 'dive-in --new do a thing'


class TestTaskModeName:
  def test_every_launch_picks_a_fresh_workspace_name(self, fake_proj, monkeypatch, capsys):
    monkeypatch.setattr(dive_in, '_prefetch_task', lambda task_id: ('My Task!', 'task block'))
    rc = dive_in.dive_in(forwarded=[], dry_run=True, task=UUID)
    assert rc == 0
    name = shlex.split(capsys.readouterr().out.strip())[-1]
    assert re.fullmatch(r'my-task-[0-9a-f]{8}', name) is not None

  def test_empty_slug_falls_back_to_dive_in(self, fake_proj, monkeypatch, capsys):
    monkeypatch.setattr(dive_in, '_prefetch_task', lambda task_id: ('!!!', 'task block'))
    rc = dive_in.dive_in(forwarded=[], dry_run=True, task=UUID)
    assert rc == 0
    name = shlex.split(capsys.readouterr().out.strip())[-1]
    assert re.fullmatch(r'dive-in-[0-9a-f]{8}', name) is not None

  def test_resume_flag_is_rejected(self):
    with pytest.raises(SystemExit):
      dive_in.main(['dive-in', '--resume', '-t', UUID])


class TestPrefetchTask:
  def test_returns_name_and_embeds_metadata_and_page(self, monkeypatch):
    from flow.model import Importance, Task

    task = Task(
      id=UUID,
      name='my task',
      status='Live',
      importance=Importance.NORMAL,
      driver=None,
      project='project-1',
      tags=['infra'],
      links=[],
      blocks=[],
      blocked_by=[],
      created_time='2026-01-01',
      last_edited='2026-01-02',
      sender=None,
      received=None,
      date=None,
      deadline=None,
      today=False,
      last_done=None,
      address=f'https://app.notion.com/p/my-task-{HEX}',
    )

    class FakeSystem:
      def get_task_info(self, task_id):
        assert task_id == UUID
        return task

      def get_page_content(self, page_id):
        assert page_id == UUID
        return '## Goal\nDo the thing.'

    monkeypatch.setattr('flow.system.default_system', lambda: FakeSystem())

    name, block = dive_in._prefetch_task(UUID)
    assert name == 'my task'
    # page body embedded verbatim
    assert '## Goal\nDo the thing.' in block
    # metadata embedded as json, enums rendered by value
    assert '"status": "Live"' in block
    assert '"importance": "Normal"' in block
    assert '"project": "project-1"' in block
    # instruction to skip the in-session fetch
    assert 'do not call get_task_info / read_page_content' in block
