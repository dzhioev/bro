#!/usr/bin/env python
import os
import re
import shlex
from datetime import UTC
from pathlib import Path
from typing import cast

import pytest

import bro.brog.system as brog_system
import bro.workspace.paths as workspace_paths
import ride.cli as ride_cli
import ride.dive_in as dive_in

UUID = '35ad38d8-5a6d-81ea-bce6-e4caf17ece7f'
HEX = '0123456789abcdef0123456789abcdef'
URL = f'https://tracker.example/tasks/my-task-{HEX}'
FRESH_SHA = 'a' * 40


def _parse_emitted(tokens: list[str]) -> tuple[dict, list[str]]:
  return ride_cli._parse(ride_cli.build_parser(), tokens)


def _workspace(tokens: list[str]) -> str:
  return tokens[tokens.index('--workspace') + 1]


def _brog_task(name: str = 'my task'):
  from bro.brog.model import Task

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
  monkeypatch.setattr(workspace_paths, 'project_root', lambda: tmp_path)
  monkeypatch.setattr(dive_in, 'project_root', lambda: tmp_path)
  (tmp_path / 'pyproject.toml').write_text('[tool.bro]\ndefault = "bro-dev"\n')
  monkeypatch.setattr(dive_in, '_fresh_origin_head', lambda _repo: FRESH_SHA)
  workspaces = workspace_paths.workspaces_dir()
  workspaces.mkdir(parents=True)
  return tmp_path


class TestLaunchCommand:
  """the emitted `ride along ...` command must parse cleanly under ride's parser."""

  @pytest.mark.parametrize(
    'kwargs',
    [
      {},
      {'forwarded': ['--host']},
      {'forwarded': ['--host', '--hold', 'guided']},
      {'command': 'do a thing', 'new': True},
    ],
  )
  def test_emitted_command_parses(self, kwargs, fake_proj, capsys):
    forwarded = kwargs.pop('forwarded', [])
    rc = dive_in.dive_in(forwarded=forwarded, dry_run=True, **kwargs)
    assert rc == 0
    tokens = shlex.split(capsys.readouterr().out.strip())
    assert tokens[0] == 'ride'
    # Parser.parse strips argv[0] as the program name, matching the console-script bridge.
    args, harness_arguments = _parse_emitted(tokens)
    assert args['cmd'] == 'along'
    assert args['repo'] == str(fake_proj)
    assert len(args['workspace']) > 0
    assert harness_arguments == []  # nothing leaked into the forwarded REMAINDER

  @pytest.mark.parametrize('host', [[], ['--host']])
  def test_an_omitted_hold_is_left_for_ride_along_to_resolve(self, host, fake_proj, capsys):
    # --host is forwarded, so the inner parse derives the same host-sensitive default
    rc = dive_in.main(['dive-in', '-n', *host])
    assert rc == 0
    tokens = shlex.split(capsys.readouterr().out.strip())
    args, harness_arguments = _parse_emitted(tokens)
    assert args['hold'] is None
    assert args['host'] == (len(host) > 0)

  def test_an_explicit_hold_is_forwarded(self, fake_proj, capsys):
    rc = dive_in.main(['dive-in', '-n', '--host', '--hold', 'attended'])
    assert rc == 0
    tokens = shlex.split(capsys.readouterr().out.strip())
    args, harness_arguments = _parse_emitted(tokens)
    assert args['hold'] == 'attended'

  def test_forwarded_flags_ride_verbatim(self, fake_proj, capsys, monkeypatch):
    monkeypatch.delenv('RIDE_BRO', raising=False)
    rc = dive_in.dive_in(forwarded=[], dry_run=True, bro='bro-dev')
    assert rc == 0
    args, harness_arguments = _parse_emitted(shlex.split(capsys.readouterr().out.strip()))
    assert args['bro'] == 'bro-dev'
    assert harness_arguments == []
    # ride owns the session theming (persona default, RIDE_BRO export); dive-in
    # must not preempt it
    assert 'RIDE_BRO' not in os.environ

  def test_harness_rides_the_forwarded_flags(self, fake_proj, capsys):
    rc = dive_in.main(['dive-in', '-n', '--harness', 'bro'])
    assert rc == 0
    args, harness_arguments = _parse_emitted(shlex.split(capsys.readouterr().out.strip()))
    assert args['harness'] == 'bro'

  def test_raw_rides_the_forwarded_flags(self, fake_proj, capsys, monkeypatch):
    monkeypatch.delenv('RIDE_BRO', raising=False)
    rc = dive_in.dive_in(forwarded=['--raw'], dry_run=True, bro='dev')
    assert rc == 0
    args, harness_arguments = _parse_emitted(shlex.split(capsys.readouterr().out.strip()))
    assert args['bro'] == 'dev'
    assert args['raw']
    assert 'RIDE_BRO' not in os.environ


class TestShellCommandReconstruction:
  """the visual banner receives the wrapper invocation, not the underlying `ride solo|along`."""

  def test_forwarded_flags_appear_in_the_reconstruction(self, fake_proj, monkeypatch):
    monkeypatch.delenv('BRO_SHELL_COMMAND', raising=False)
    rc = dive_in.main(['dive-in', '-n', '--hold', 'guided', '--bro', 'bro-dev'])
    assert rc == 0
    assert os.environ['BRO_SHELL_COMMAND'] == 'dive-in --hold guided --bro bro-dev'

  def test_new_seed_keeps_the_prompt_marker_tail(self, fake_proj, monkeypatch):
    monkeypatch.delenv('BRO_SHELL_COMMAND', raising=False)
    rc = dive_in.main(['dive-in', '-n', '--new', 'do a thing'])
    assert rc == 0
    # `ride banner` splits the user prompt off at the last ` --new ` marker
    assert os.environ['BRO_SHELL_COMMAND'] == 'dive-in --new do a thing'


class TestBaseRef:
  """an omitted --into resolves to origin's fresh HEAD; explicit values pass through."""

  def test_omitted_into_forwards_the_fetched_sha(self, fake_proj, capsys):
    rc = dive_in.main(['dive-in', '-n'])
    assert rc == 0
    args, harness_arguments = _parse_emitted(shlex.split(capsys.readouterr().out.strip()))
    assert args['into'] == FRESH_SHA

  def test_explicit_into_skips_the_fetch(self, fake_proj, capsys, monkeypatch):
    monkeypatch.setattr(dive_in, '_fresh_origin_head', lambda _repo: pytest.fail('must not fetch'))
    rc = dive_in.main(['dive-in', '-n', '--into', 'feature'])
    assert rc == 0
    args, harness_arguments = _parse_emitted(shlex.split(capsys.readouterr().out.strip()))
    assert args['into'] == 'feature'

  def test_unreachable_origin_falls_back_to_the_host_head(self, fake_proj, capsys, monkeypatch):
    monkeypatch.setattr(dive_in, '_fresh_origin_head', lambda _repo: None)
    rc = dive_in.main(['dive-in', '-n'])
    assert rc == 0
    args, harness_arguments = _parse_emitted(shlex.split(capsys.readouterr().out.strip()))
    assert args['into'] is None

  def test_resolved_sha_stays_out_of_the_shell_command(self, fake_proj, monkeypatch):
    monkeypatch.delenv('BRO_SHELL_COMMAND', raising=False)
    rc = dive_in.main(['dive-in', '-n'])
    assert rc == 0
    assert os.environ['BRO_SHELL_COMMAND'] == 'dive-in'


class TestNewMode:
  def test_without_seed_uses_spell_command(self, fake_proj, capsys):
    rc = dive_in.dive_in(forwarded=[], dry_run=True, new=True)
    assert rc == 0
    tokens = shlex.split(capsys.readouterr().out.strip())
    assert tokens[-1] == '[[fix --new ""]]'

  def test_seed_stays_inside_spell_command(self, fake_proj, capsys):
    rc = dive_in.dive_in(forwarded=[], dry_run=True, new=True, command='do a thing')
    assert rc == 0
    tokens = shlex.split(capsys.readouterr().out.strip())
    assert tokens[-1] == '[[fix --new do a thing]]'

  def test_raw_flavor_uses_the_same_spell_command(self, fake_proj, capsys):
    rc = dive_in.dive_in(
      forwarded=['--raw'], dry_run=True, new=True, bro='bro-dev', command='do a thing'
    )
    assert rc == 0
    tokens = shlex.split(capsys.readouterr().out.strip())
    assert tokens[-1] == '[[fix --new do a thing]]'


class TestTaskMode:
  @pytest.fixture(autouse=True)
  def fake_backend(self, monkeypatch):
    monkeypatch.setattr(
      dive_in, '_task_system', lambda repo, grant, revoke, bro, harness, harness_options: object()
    )

  def test_every_launch_picks_a_fresh_workspace_name(self, fake_proj, monkeypatch, capsys):
    monkeypatch.setattr(
      dive_in, '_prefetch_task', lambda system, ref: (_brog_task('My Task!'), 'task block')
    )
    rc = dive_in.dive_in(forwarded=[], dry_run=True, task=UUID)
    assert rc == 0
    name = _workspace(shlex.split(capsys.readouterr().out.strip()))
    assert re.fullmatch(r'my-task-[0-9a-f]{8}', name) is not None

  def test_empty_slug_falls_back_to_dive_in(self, fake_proj, monkeypatch, capsys):
    monkeypatch.setattr(
      dive_in, '_prefetch_task', lambda system, ref: (_brog_task('!!!'), 'task block')
    )
    rc = dive_in.dive_in(forwarded=[], dry_run=True, task=UUID)
    assert rc == 0
    name = _workspace(shlex.split(capsys.readouterr().out.strip()))
    assert re.fullmatch(r'dive-in-[0-9a-f]{8}', name) is not None

  def test_seeds_fix_with_the_original_ref_and_exports_the_canonical_id(
    self, fake_proj, monkeypatch, capsys
  ):
    monkeypatch.setattr(dive_in, '_prefetch_task', lambda system, ref: (_brog_task(), 'task block'))
    rc = dive_in.dive_in(forwarded=[], dry_run=True, task=URL)
    assert rc == 0
    tokens = shlex.split(capsys.readouterr().out.strip())
    prompt = tokens[-1]
    assert prompt.startswith(f'[[fix {URL}]]\n\ntask block')
    assert os.environ['RIDE_TASK_ID'] == UUID

  def test_raw_flavor_keeps_prefetch_and_appended_command_outside_spell_command(
    self, fake_proj, monkeypatch, capsys
  ):
    monkeypatch.setattr(dive_in, '_prefetch_task', lambda system, ref: (_brog_task(), 'task block'))
    rc = dive_in.dive_in(
      forwarded=['--raw', '--bro', 'bro-dev'],
      dry_run=True,
      task=URL,
      command='run the focused checks',
    )
    assert rc == 0
    tokens = shlex.split(capsys.readouterr().out.strip())
    prompt = tokens[-1]
    assert prompt == (
      f'[[fix {URL}]]\n\ntask block\n\nOnce you understand the task, run the focused checks'
    )

  def test_prefetch_binds_the_launch_scope_flags(self, fake_proj, monkeypatch, capsys):
    captured = {}

    def fake_task_system(repo, grant, revoke, bro, harness, harness_options):
      captured.update(
        grant=grant, revoke=revoke, bro=bro, harness=harness, harness_options=harness_options
      )
      return object()

    monkeypatch.setattr(dive_in, '_task_system', fake_task_system)
    monkeypatch.setattr(dive_in, '_prefetch_task', lambda system, ref: (_brog_task(), 'task block'))
    argv = ['dive-in', '-n', '-t', UUID, '--grant', 'brog+github']
    rc = dive_in.main([*argv, '--bro', 'dev', '--raw'])
    assert rc == 0
    assert captured == {
      'grant': ['brog+github'],
      'revoke': [],
      'bro': 'dev',
      'harness': 'claude',
      'harness_options': {'raw': True},
    }
    # the flags still ride into the forwarded `ride along` untouched
    args, harness_arguments = _parse_emitted(shlex.split(capsys.readouterr().out.strip()))
    assert args['grant'] == ['brog+github']
    assert args['revoke'] is None

  def test_scope_without_brog_fails_before_any_launch(self, fake_proj, monkeypatch, capsys):
    from bro.base import credentials

    def no_brog(repo, grant, revoke, bro, harness, harness_options):
      raise credentials.SecretNotFound('brog')

    monkeypatch.setattr(dive_in, '_task_system', no_brog)
    rc = dive_in.dive_in(forwarded=[], dry_run=True, task=UUID, revoke=['brog'])
    assert rc == 1
    assert capsys.readouterr().out == ''

  def test_bad_scope_override_fails_before_any_launch(self, fake_proj, monkeypatch):
    from ride.scope import LaunchScopeError

    def bad_override(repo, grant, revoke, bro, harness, harness_options):
      raise LaunchScopeError("cannot grant 'brog': already in the scoped credential set")

    monkeypatch.setattr(dive_in, '_task_system', bad_override)
    rc = dive_in.dive_in(forwarded=[], dry_run=True, task=UUID, grant=['brog'])
    assert rc == 1

  def test_focus_flag_is_rejected(self):
    with pytest.raises(SystemExit):
      dive_in.main(['dive-in', '--focus'])

  def test_resume_flag_is_rejected(self):
    with pytest.raises(SystemExit):
      dive_in.main(['dive-in', '--resume', '-t', UUID])


class TestTaskSystem:
  """the prefetch backend reads `brog` through the launch's own scope binding."""

  def _fake_wiring(self, monkeypatch, calls: dict):
    from types import SimpleNamespace

    class FakeStore:
      def get_json(self, name):
        calls['read'] = name
        return {'backend': 'github', 'token': 't', 'repo': 'owner/repo'}

    monkeypatch.setattr(
      dive_in, 'project_config', lambda _repo: SimpleNamespace(default_bro='bro-dev')
    )

    def fake_scoped_secrets(bro_name, surface, *, attachment):
      calls['scoped'] = (bro_name, surface)
      return 'base-scope'

    monkeypatch.setattr(dive_in, 'scoped_secrets', fake_scoped_secrets)

    def fake_view(scoped, *, grant, revoke):
      calls['view'] = (scoped, grant, revoke)
      return FakeStore()

    monkeypatch.setattr(dive_in, 'launch_view_store', fake_view)

  def test_reads_brog_through_the_launch_view(self, monkeypatch):
    import bro.brog.github as brog_github
    from ride.claude.harness import CLAUDE

    calls: dict = {}
    self._fake_wiring(monkeypatch, calls)
    system = dive_in._task_system(
      Path('/repo'), ['brog+github'], [], None, 'claude', {'raw': False}
    )
    assert calls['scoped'] == ('bro-dev', CLAUDE.scope_recipe({'raw': False}))
    assert calls['view'] == ('base-scope', ['brog+github'], [])
    assert calls['read'] == 'brog'
    assert isinstance(system, brog_github.System)

  def test_raw_flavor_scopes_the_raw_surface_and_explicit_bro_wins(self, monkeypatch):
    from ride.claude.harness import CLAUDE

    calls: dict = {}
    self._fake_wiring(monkeypatch, calls)
    dive_in._task_system(Path('/repo'), [], [], 'dev', 'claude', {'raw': True})
    assert calls['scoped'] == ('dev', CLAUDE.scope_recipe({'raw': True}))

  def test_bro_harness_scopes_the_native_recipe(self, monkeypatch):
    from ride.scope import BRO_RUN_RECIPE

    calls: dict = {}
    self._fake_wiring(monkeypatch, calls)
    dive_in._task_system(Path('/repo'), [], [], None, 'bro', {})
    assert calls['scoped'] == ('bro-dev', BRO_RUN_RECIPE)


class TestPrefetchTask:
  def test_returns_task_and_embeds_metadata_description_and_comments(self):
    from datetime import datetime

    from bro.brog.model import Comment, Project, Task

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
            author='bro-dev',
            timestamp=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            body='the plan',
          )
        ]

    got, block = dive_in._prefetch_task(cast(brog_system.System, FakeSystem()), URL)
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
