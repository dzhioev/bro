import json
from typing import Optional
from unittest.mock import patch

import cw.session
from cw.secrets import ScopedSecrets


def _spec(
  *,
  name: str = 'w',
  container: bool = True,
  drop: bool = False,
  auto: bool = False,
  fast: bool = False,
  grant: Optional[list[str]] = None,
  revoke: Optional[list[str]] = None,
  effort: Optional[str] = None,
  resume: bool = False,
  into: Optional[str] = None,
  mcp: Optional[str] = None,
  bro: Optional[str] = None,
  prompt: Optional[str] = None,
  claude_args: Optional[list[str]] = None,
) -> cw.session.SessionSpec:
  return cw.session.SessionSpec(
    name=name,
    container=container,
    drop=drop,
    auto=auto,
    fast=fast,
    grant=grant if grant is not None else [],
    revoke=revoke if revoke is not None else [],
    effort=effort,
    resume=resume,
    into=into,
    mcp=mcp,
    bro=bro,
    prompt=prompt,
    claude_args=claude_args if claude_args is not None else [],
  )


class TestResolveBaseRef:
  def _patch(self, monkeypatch, *, local_rc, fetch_rc=1, fetched_sha='deadbeef'):
    import pathlib
    from types import SimpleNamespace

    monkeypatch.setattr(cw.session, '_project_root', lambda: pathlib.Path('/repo'))
    calls: list = []

    def fake_run(args, **kwargs):
      calls.append(args)
      if args[:3] == ['git', 'rev-parse', '--verify'] and args[3] == 'FETCH_HEAD^{commit}':
        return SimpleNamespace(returncode=0, stdout=f'{fetched_sha}\n')
      if args[:3] == ['git', 'rev-parse', '--verify']:
        return SimpleNamespace(returncode=local_rc, stdout='localsha\n' if local_rc == 0 else '')
      if args[:2] == ['git', 'fetch']:
        return SimpleNamespace(returncode=fetch_rc, stdout='')
      raise AssertionError(f'unexpected command {args}')

    monkeypatch.setattr(cw.session.subprocess, 'run', fake_run)
    return calls

  def test_resolves_host_local_ref_without_fetching(self, monkeypatch):
    calls = self._patch(monkeypatch, local_rc=0)
    assert cw.session._resolve_base_ref('master') == 'localsha'
    assert not any(c[:2] == ['git', 'fetch'] for c in calls)

  def test_fetches_origin_when_ref_not_host_local(self, monkeypatch):
    calls = self._patch(monkeypatch, local_rc=1, fetch_rc=0, fetched_sha='abc123')
    assert cw.session._resolve_base_ref('worktree-feature') == 'abc123'
    assert ['git', 'fetch', 'origin', 'worktree-feature'] in calls

  def test_returns_none_when_neither_resolves(self, monkeypatch):
    self._patch(monkeypatch, local_rc=1, fetch_rc=1)
    assert cw.session._resolve_base_ref('nope') is None


class TestGrantRevoke:
  def _start(
    self,
    *,
    grant: Optional[list[str]] = None,
    revoke: Optional[list[str]] = None,
    effort: Optional[str] = None,
  ) -> int:
    return cw.session.start_session(_spec(drop=True, grant=grant, revoke=revoke, effort=effort))

  def test_start_session_applies_grant_and_revoke(self):
    with (
      patch.dict('os.environ', {}, clear=False) as env,
      patch('cw.session.cw', return_value=0) as fake_cw,
      patch(
        'cw.session._container_secrets',
        return_value=ScopedSecrets({'notion', 'trails', 'github'}, set(), True),
      ),
      patch('cw.session._session_append_prompt', return_value=''),
    ):
      env.pop('CW_BRO', None)
      env.pop('CW_IN_CONTAINER', None)
      rc = self._start(grant=['gmail_creds'], revoke=['notion'])
    assert rc == 0
    _, kwargs = fake_cw.call_args
    assert 'gmail_creds' in kwargs['secrets']
    assert 'notion' not in kwargs['secrets']

  def test_start_session_grant_already_present_returns_1(self):
    with (
      patch.dict('os.environ', {}, clear=False) as env,
      patch('cw.session.cw', return_value=0) as fake_cw,
      patch('cw.session._container_secrets', return_value=ScopedSecrets({'github'}, set(), True)),
      patch('cw.session._session_append_prompt', return_value=''),
    ):
      env.pop('CW_BRO', None)
      env.pop('CW_IN_CONTAINER', None)
      rc = self._start(grant=['github'])
    assert rc == 1
    assert fake_cw.call_count == 0

  def test_start_session_injects_effort_into_claude_args(self):
    with (
      patch.dict('os.environ', {}, clear=False) as env,
      patch('cw.session.cw', return_value=0) as fake_cw,
      patch('cw.session._container_secrets', return_value=ScopedSecrets({'github'}, set(), True)),
      patch('cw.session._session_append_prompt', return_value=''),
    ):
      env.pop('CW_BRO', None)
      env.pop('CW_IN_CONTAINER', None)
      rc = self._start(effort='xhigh')
    assert rc == 0
    _, kwargs = fake_cw.call_args
    claude_args = kwargs['claude_args']
    idx = claude_args.index('--effort')
    assert claude_args[idx + 1] == 'xhigh'


class TestContainerLocalMCP:
  def test_cw_wires_entrypoint_env_and_mcp_config(self):
    with (
      patch.dict('os.environ', {}, clear=False) as env,
      patch('cw.session.run_in_container', return_value=0) as run_in_container,
    ):
      env.pop('CW_IN_CONTAINER', None)
      rc = cw.session.cw(_spec(mcp='local', drop=True), claude_args=['--foo'], secrets=set())
    assert rc == 0
    args, kwargs = run_in_container.call_args
    command = args[1]
    extra_env = kwargs['extra_env']
    assert extra_env['CW_MCP_HTTP_SPEC'] == 'flow'
    i = command.index('--mcp-config')
    entry = json.loads(command[i + 1])['mcpServers']['flow']
    assert entry['url'] == f'http://127.0.0.1:{extra_env["CW_MCP_HTTP_PORT"]}/flow'
    assert entry['headers'] == {'Authorization': f'Bearer {extra_env["CW_MCP_HTTP_TOKEN"]}'}

  def test_no_mcp_session_passes_no_mcp_env(self):
    with (
      patch.dict('os.environ', {}, clear=False) as env,
      patch('cw.session.run_in_container', return_value=0) as run_in_container,
    ):
      env.pop('CW_IN_CONTAINER', None)
      rc = cw.session.cw(_spec(mcp=None, drop=True), claude_args=[], secrets=set())
    assert rc == 0
    _, kwargs = run_in_container.call_args
    assert kwargs['extra_env'] is None


class TestResumeCommand:
  def test_create_command_includes_drop_into_and_claude_args(self):
    parts = _spec(
      container=True,
      auto=True,
      fast=True,
      drop=True,
      effort='xhigh',
      mcp='http',
      grant=['gmail_creds'],
      revoke=['notion'],
      into='feature',
      claude_args=['--foo'],
    ).to_command_argv()
    assert parts == [
      'cw', 'ss', '-c', '--auto', '--fast', '--drop',
      '--effort', 'xhigh', '--mcp', '--grant', 'gmail_creds',
      '--revoke', 'notion', '--into', 'feature', 'w', '--foo',
    ]  # fmt: skip

  def test_resume_variant_carries_forwarded_flags_and_clears_create_only(self):
    # resume_variant keeps --auto/--effort/--mcp/--grant and adds --resume, while
    # clearing the create-only --drop/--into/prompt/claude args
    parts = (
      _spec(
        container=True,
        auto=True,
        drop=True,
        effort='xhigh',
        mcp='http',
        grant=['gmail_creds'],
        into='feature',
        prompt='do it',
        claude_args=['--foo'],
      )
      .resume_variant()
      .to_command_argv()
    )
    assert parts == [
      'cw', 'ss', '-c', '--auto', '--resume',
      '--effort', 'xhigh', '--mcp', '--grant', 'gmail_creds', 'w',
    ]  # fmt: skip

  def test_start_session_records_resume_command(self):
    with (
      patch.dict('os.environ', {}, clear=False) as env,
      patch('cw.session.cw', return_value=0),
      patch('cw.session._container_secrets', return_value=ScopedSecrets({'github'}, set(), True)),
      patch('cw.session._session_append_prompt', return_value=''),
    ):
      env.pop('CW_BRO', None)
      env.pop('CW_IN_CONTAINER', None)
      cw.session.start_session(
        _spec(
          container=True, drop=True, auto=True, grant=['gmail_creds'], effort='xhigh', mcp='http'
        )
      )
      resume_command = env['CW_RESUME_COMMAND']
    assert resume_command == 'cw ss -c --auto --resume --effort xhigh --mcp --grant gmail_creds w'
