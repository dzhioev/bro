import contextlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import ride.cli as ride_cli
from bro.base import configs, credentials
from bro.llm.llms.echo import LLMSpec as EchoLLMSpec
from bro.workspace.paths import workspace_dir
from bros.bro import Bro
from ride import pending_launch
from ride.bro_worker import pending_bro
from ride.do_ride import command as do_ride_command
from ride.harness import get_harness
from ride.workspace.metadata import Isolation


class HydrationBro(Bro):
  name = 'hydration-test'
  description = 'credential hydration route fixture'
  extra_secrets = ('github',)
  llm_spec = EchoLLMSpec()


@pytest.fixture(autouse=True)
def project(monkeypatch):
  monkeypatch.setattr(
    ride_cli,
    'project_config',
    lambda _repo=None: SimpleNamespace(
      default_bro='bro-dev',
      harness='claude',
      summon_harness=configs.DEFAULT_SUMMON_HARNESS,
      summon_depth=configs.DEFAULT_SUMMON_DEPTH,
    ),
  )
  monkeypatch.setattr(ride_cli, 'fresh_workspace_name', lambda base: f'{base}-12345678')
  monkeypatch.setattr(ride_cli, 'reexec_from_runtime', lambda _runtime, _argv: None)


def _session_command(spec) -> list[str]:
  return do_ride_command(spec)


class TestSolo:
  def test_builds_an_unattended_claude_session(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'solo', 'dev', 'do it']) == 0
    spec = start.call_args.args[0]
    assert spec.harness == 'claude'
    assert spec.name == 'ride-dev-12345678'
    assert spec.bro == 'dev'
    assert spec.prompt == 'do it'
    assert spec.hold == 'unattended'
    assert spec.solo
    assert spec.drop
    assert not spec.workspace_pinned
    assert _session_command(spec)[:6] == [
      'do-ride',
      'solo',
      '--workspace',
      'ride-dev-12345678',
      '--harness',
      'claude',
    ]

  def test_credential_pick_reaches_the_spec(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'solo', '--cred', 'github+work', 'dev', 'do it']) == 0
    assert start.call_args.args[0].cred == ['github+work']

  def test_env_additions_reach_the_spec_in_order(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      argv = ['ride', 'solo', '--env', 'IS_SANDBOX=1', '--env', 'PAIR=a=b', 'dev', 'do it']
      assert ride_cli.main(argv) == 0
    assert start.call_args.args[0].env == {'IS_SANDBOX': '1', 'PAIR': 'a=b'}

  @pytest.mark.parametrize('addition', ['NOVALUE', '1BAD=x', 'A-B=x'])
  def test_a_malformed_env_addition_is_a_cli_error(self, addition, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'solo', '--env', addition, 'dev', 'do it'])
    assert '--env takes NAME=VALUE' in capsys.readouterr().err

  def test_a_repeated_env_name_is_a_cli_error(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'solo', '--env', 'A=1', '--env', 'A=2', 'dev', 'do it'])
    assert '--env names A twice' in capsys.readouterr().err

  def test_session_log_is_recorded_as_the_level_addition(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      argv = ['ride', 'solo', '--session-log', 'verbose', '--env', 'A=1', 'dev', 'do it']
      assert ride_cli.main(argv) == 0
    assert start.call_args.args[0].env == {'A': '1', 'BRO_LOG_LEVEL': 'VERBOSE'}

  def test_session_log_and_an_env_addition_of_the_level_clash(self, capsys):
    argv = ['ride', 'solo', '--session-log', 'verbose', '--env', 'BRO_LOG_LEVEL=DEBUG', 'dev', 'x']
    with pytest.raises(SystemExit):
      ride_cli.main(argv)
    assert 'both set the session log level' in capsys.readouterr().err

  def test_unboxed_keeps_the_unattended_default(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'solo', '--unboxed', 'dev', 'do it'])
    assert start.call_args.args[0].hold == 'unattended'
    assert start.call_args.args[0].isolation is Isolation.UNBOXED

  def test_boxed_is_the_default_and_has_an_explicit_spelling(self):
    for flags in ([], ['--boxed']):
      with patch('ride.cli.start_session', return_value=0) as start:
        ride_cli.main(['ride', 'solo', *flags, 'dev', 'do it'])
      assert start.call_args.args[0].isolation is Isolation.BOXED

  def test_keep_retains_an_automatic_workspace(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'solo', '--keep', 'dev', 'do it'])
    assert not start.call_args.args[0].drop

  def test_pinned_workspace_is_retained(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'solo', '--workspace', 'shared', 'dev', 'do it'])
    spec = start.call_args.args[0]
    assert spec.name == 'shared'
    assert spec.workspace_pinned
    assert not spec.drop

  def test_pinned_workspace_rejects_keep(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'solo', '--workspace', 'shared', '--keep', 'dev', 'do it'])
    assert 'pinned workspaces are always kept' in capsys.readouterr().err

  def test_forwards_arguments_only_after_the_separator(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'solo', 'dev', 'hello', '--', '--debug', 'mcp'])
    assert start.call_args.args[0].arguments == ['--debug', 'mcp']

  def test_no_trails_stays_an_outer_launch_setting(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'solo', '--harness', 'bro', '--no-trails', 'dev', 'hello']) == 0
    spec = start.call_args.args[0]
    assert spec.no_trails
    assert '--no-trails' not in _session_command(spec)


class TestAttachment:
  def test_mode_launches_detached_by_default(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', 'dev']) == 0
    spec = start.call_args.args[0]
    assert spec.repo is None
    assert spec.harness == 'claude'

  def test_detached_launch_resolves_depth_without_a_project(self):
    with (
      patch('ride.cli.host_config.summon_depth', return_value=6) as resolve_depth,
      patch('ride.cli.start_session', return_value=0) as start,
    ):
      assert ride_cli.main(['ride', 'along', 'dev']) == 0

    resolve_depth.assert_called_once_with(None)
    assert start.call_args.args[0].summon_depth == 6

  def test_detached_launch_summons_under_the_default_harness(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', 'dev']) == 0
    assert start.call_args.args[0].summon_harness == configs.DEFAULT_SUMMON_HARNESS

  def test_repo_resolves_any_directory_inside_the_checkout(self, monkeypatch):
    monkeypatch.setattr(ride_cli, 'project_root', lambda path: Path('/repo'))
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--repo', '/repo/subdir', 'dev']) == 0
    assert start.call_args.args[0].repo == '/repo'

  def test_git_url_is_preserved_in_the_session_spec(self, monkeypatch):
    url = 'https://example.test/owner/repo.git'
    repository = SimpleNamespace(
      identity=url,
      is_url=True,
      project_config=lambda: SimpleNamespace(
        default_bro='bro-dev',
        harness='claude',
        summon_harness=configs.DEFAULT_SUMMON_HARNESS,
        summon_depth=configs.DEFAULT_SUMMON_DEPTH,
        sections={},
      ),
    )
    monkeypatch.setattr(ride_cli, '_resolve_repository_argument', lambda _value: repository)
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--repo', url, 'dev']) == 0
    spec, resolved = start.call_args.args
    assert resolved is repository
    assert spec.repo == url

  def test_into_requires_an_attachment(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--into', 'feature', 'dev'])
    assert '--into requires --repo' in capsys.readouterr().err

  def test_external_tree_is_recorded_as_an_absolute_path(self, tmp_path):
    tree = tmp_path / 'task'
    tree.mkdir()
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'solo', '--unboxed', '--tree', str(tree), 'dev', 'work']) == 0
    assert start.call_args.args[0].tree == str(tree.resolve())

  @pytest.mark.parametrize('flags', [[], ['--boxed'], ['--unboxed', '--repo', '.']])
  def test_external_tree_requires_a_detached_unboxed_launch(self, tmp_path, flags, capsys):
    tree = tmp_path / 'task'
    tree.mkdir()
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'solo', *flags, '--tree', str(tree), 'dev', 'work'])
    assert '--tree' in capsys.readouterr().err

  def test_given_runtime_is_recorded_as_an_absolute_path(self, tmp_path):
    runtime = tmp_path / 'runtime'
    with patch('ride.cli.start_session', return_value=0) as start:
      assert (
        ride_cli.main(
          ['ride', 'solo', '--unboxed', '--runtime-bundle', str(runtime), 'dev', 'work']
        )
        == 0
      )
    assert start.call_args.args[0].runtime_bundle == str(runtime.resolve())


class TestHostLLMDefault:
  @pytest.fixture(autouse=True)
  def _checkout(self, monkeypatch, tmp_path):
    monkeypatch.setattr(ride_cli, 'project_root', lambda path: Path('/repo'))
    self.config = tmp_path / 'bro.json'
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(self.config))

  def _entry(self, llm: str) -> None:
    self.config.write_text(json.dumps({'projects': {'/repo': {'bros': {'dev': {'llm': llm}}}}}))

  def test_the_launch_records_the_recipe_settled_over_the_hosts_entry(self):
    self._entry('openai:sol:xhigh')
    with patch('ride.cli.start_session', return_value=0) as start:
      argv = ['ride', 'along', '--repo', '/repo', '--harness', 'bro', '--effort', 'low', 'dev']
      assert ride_cli.main(argv) == 0
    spec = start.call_args.args[0]
    assert spec.llm == 'openai:sol:low'
    assert spec.resolved_llm == get_harness('bro').resolve_llm('openai:sol:low', 'dev').dump()
    command = _session_command(spec)
    assert command[command.index('--llm') + 1] == 'openai:sol:low'

  def test_a_recipe_the_harness_cannot_run_fails_the_launch(self, capsys):
    self._entry('openai:sol')
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--repo', '/repo', 'dev'])
    assert '--harness bro' in capsys.readouterr().err

  def test_a_malformed_entry_names_itself(self, capsys):
    self._entry('::ludicrous')
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--repo', '/repo', 'dev'])
    assert "bros.dev.llm '::ludicrous' (project-path-bro)" in capsys.readouterr().err


class TestHostConfigScopeErrors:
  @pytest.fixture(autouse=True)
  def config_file(self, tmp_path, monkeypatch):
    self.path = tmp_path / 'bro.json'
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(self.path))

  def _preflight(self, spec, *_args, **_kwargs):
    from ride.scope import LaunchScopeError, scoped_secrets

    try:
      scoped_secrets(
        spec.bro,
        get_harness(spec.harness).scope_recipe(),
        cred=spec.cred,
        grant=spec.grant,
        revoke=spec.revoke,
        llm_spec=spec.llm_spec,
      )
    except LaunchScopeError as error:
      from bro.base import log

      log.error('%s', error)
      return 1
    return 0

  def test_instance_spelled_config_grant_names_the_rollout_replacement(self, capsys):
    self.path.write_text(json.dumps({'defaults': {'grant': ['github+reviewer']}}))

    with (
      patch('ride.cli.start_session', side_effect=self._preflight),
      pytest.raises(SystemExit),
    ):
      ride_cli.main(['ride', 'solo', '--harness', 'bro', 'dev', 'work'])

    error = capsys.readouterr().err
    assert 'defaults' in error
    assert '"creds": ["github+reviewer"]' in error
    assert '"grant": ["github"]' in error

  def test_unregistered_default_names_the_project_entry_replacement(self, caplog):
    self.path.write_text(json.dumps({'defaults': {'creds': ['consumer_only+work']}}))

    with patch('ride.cli.start_session', side_effect=self._preflight):
      code = ride_cli.main(['ride', 'solo', '--harness', 'bro', 'dev', 'work'])

    assert code == 1
    assert 'defaults names unregistered credential kind(s): consumer_only' in caplog.text
    assert 'move host-wide picks or scope changes into the project entries' in caplog.text


class TestCredentialHydrationRoutes:
  @pytest.fixture
  def route(self, tmp_path, monkeypatch, register_test_bros):
    register_test_bros(HydrationBro)
    repository = tmp_path / 'repo'
    repository.mkdir()
    (repository / 'pyproject.toml').write_text(
      '[tool.bro]\ndefault = "hydration-test"\ngrant = ["aws"]\nrevoke = ["openai"]\n'
    )
    subprocess.run(['git', 'init', '-q', repository], check=True)
    subprocess.run(['git', '-C', repository, 'config', 'user.name', 'Test User'], check=True)
    subprocess.run(
      ['git', '-C', repository, 'config', 'user.email', 'test@example.com'], check=True
    )
    subprocess.run(['git', '-C', repository, 'add', 'pyproject.toml'], check=True)
    subprocess.run(['git', '-C', repository, 'commit', '-qm', 'test project'], check=True)

    store = tmp_path / 'store'
    material = store / credentials.MATERIAL_DIR
    material.mkdir(parents=True)

    def write(name: str, value: str) -> None:
      (material / f'{name}{credentials.MATERIAL_SUFFIX}').write_text(value)

    for name, value in {
      'github+project': 'github-project',
      'github+resume': 'github-resume',
      'aws+project': 'aws-project',
      'brog+bro': 'brog-bro',
      'harbor+launch': 'harbor-launch',
    }.items():
      write(name, value)

    config = tmp_path / 'bro.json'
    config.write_text(
      json.dumps(
        {
          'defaults': {
            'creds': [
              'github+default',
              'aws+default',
              'brog+default',
              'harbor+default',
            ],
            'grant': ['brog', 'harbor'],
          },
          'projects': {
            str(repository): {
              'creds': ['github+project', 'aws+project'],
              'revoke': ['brog'],
              'bros': {
                'hydration-test': {
                  'creds': ['brog+bro'],
                  'grant': ['brog'],
                }
              },
            }
          },
        }
      )
    )
    monkeypatch.setattr(credentials, 'STORE_DIR', str(store))
    monkeypatch.setattr('bro.base.host_config.HOST_CONFIG_FILE', str(config))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'state'))

    class Runtime:
      reference = 'test-runtime'

      def materialize_host(self) -> None:
        pass

    @contextlib.contextmanager
    def runtime_bundle():
      yield Runtime()

    monkeypatch.setattr('ride.session.resolve_runtime_bundle', runtime_bundle)
    monkeypatch.setattr('ride.session.reexec_from_runtime', lambda reference, argv: None)
    captures = []

    def capture_launch(spec, workspace, base_ref, launch_scope, **kwargs):
      captures.append((spec, launch_scope))
      return 0

    monkeypatch.setattr('ride.session._launch_session', capture_launch)

    def launch(*scope_flags: str) -> int:
      result = ride_cli.main(
        [
          'ride',
          'solo',
          '--unboxed',
          '--workspace',
          'hydration-route',
          '--repo',
          str(repository),
          '--harness',
          'bro',
          *scope_flags,
          'hydration-test',
          'work',
        ]
      )
      assert result is not None
      return result

    return SimpleNamespace(material=material, captures=captures, launch=launch)

  def test_root_launch_folds_every_layer_and_hydrates_by_selected_name(self, route):
    assert route.launch('--cred', 'harbor+launch', '--grant', 'github', '--revoke', 'openai') == 0

    spec, launch_scope = route.captures[-1]
    assert spec.cred == ['harbor+launch']
    assert launch_scope.scoped.required == {'github', 'aws', 'brog', 'harbor'}
    assert launch_scope.scoped.optional == {'trails'}
    assert launch_scope.store['creds/github.cred'] == b'github-project'
    assert launch_scope.store['creds/aws.cred'] == b'aws-project'
    assert launch_scope.store['creds/brog.cred'] == b'brog-bro'
    assert launch_scope.store['creds/harbor.cred'] == b'harbor-launch'
    assert 'creds/trails.cred' not in launch_scope.store

  def test_root_launch_fails_for_a_picked_absent_instance(self, route, caplog):
    assert route.launch('--cred', 'harbor+absent') == 1

    assert route.captures == []
    assert "secret 'harbor+absent' not found" in caplog.text

  def test_root_launch_fails_when_a_present_name_cannot_load(self, route, caplog):
    path = route.material / f'harbor+broken{credentials.MATERIAL_SUFFIX}'
    path.write_bytes(b'\xff')

    assert route.launch('--cred', 'harbor+broken') == 1

    assert route.captures == []
    assert 'harbor+broken.cred is not valid UTF-8 text' in caplog.text

  def test_resume_merges_scope_flags_before_hydrating(self, route):
    assert route.launch('--cred', 'harbor+launch') == 0

    assert (
      ride_cli.main(
        [
          'ride',
          'resume',
          '--cred',
          'github+resume',
          '--revoke',
          'aws',
          '--grant',
          'brog',
          'hydration-route',
        ]
      )
      == 0
    )

    spec, launch_scope = route.captures[-1]
    assert spec.cred == ['harbor+launch', 'github+resume']
    assert spec.revoke == ['aws']
    assert launch_scope.scoped.required == {'github', 'brog', 'harbor'}
    assert launch_scope.store['creds/github.cred'] == b'github-resume'
    assert 'creds/aws.cred' not in launch_scope.store


class TestAlong:
  def test_builds_an_attended_claude_session(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', 'dev', 'do it']) == 0
    spec = start.call_args.args[0]
    assert spec.harness == 'claude'
    assert spec.name == 'ride-dev-12345678'
    assert spec.bro == 'dev'
    assert spec.prompt == 'do it'
    assert spec.hold == 'attended'
    assert not spec.drop
    assert not spec.workspace_pinned
    assert _session_command(spec)[:6] == [
      'do-ride',
      'along',
      '--workspace',
      'ride-dev-12345678',
      '--harness',
      'claude',
    ]

  def test_unboxed_defaults_to_guided(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'along', '--unboxed', 'dev'])
    assert start.call_args.args[0].hold == 'guided'

  def test_workspace_pins_an_existing_name(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'along', '--workspace', 'shared', 'dev'])
    spec = start.call_args.args[0]
    assert spec.name == 'shared'
    assert spec.workspace_pinned

  def test_pinned_workspace_rejects_drop(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--workspace', 'shared', '--drop', 'dev'])
    assert 'pinned workspaces are always kept' in capsys.readouterr().err

  def test_forwards_arguments_only_after_the_separator(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'along', 'dev', 'hello', '--', '--debug', 'mcp'])
    assert start.call_args.args[0].arguments == ['--debug', 'mcp']

  def test_forwarded_arguments_reach_the_bro_harness_too(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--harness', 'bro', 'dev', '--', '--fork']) == 0
    spec = start.call_args.args[0]
    assert spec.arguments == ['--fork']
    assert _session_command(spec) == [
      'do-ride', 'along', '--workspace', 'ride-dev-12345678', '--harness', 'bro',
      '--hold', 'attended', 'dev', '--', '--fork',
    ]  # fmt: skip

  def test_incompatible_provider_names_the_harness_remedy(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--provider', 'openai', 'dev'])
    assert '--harness bro' in capsys.readouterr().err

  def test_an_unknown_bro_is_named_on_either_harness(self, capsys):
    for harness in ('claude', 'bro'):
      with pytest.raises(SystemExit):
        ride_cli.main(['ride', 'along', '--harness', harness, 'no-such-bro'])
      assert "unknown bro: 'no-such-bro'" in capsys.readouterr().err

  def test_bro_harness_builds_a_native_chat(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--harness', 'bro', 'dev']) == 0
    spec = start.call_args.args[0]
    assert spec.harness == 'bro'
    assert _session_command(spec) == [
      'do-ride', 'along', '--workspace', 'ride-dev-12345678', '--harness', 'bro',
      '--hold', 'attended', 'dev',
    ]  # fmt: skip

  def test_attached_launch_passes_project_depth_to_host_resolution(self, monkeypatch):
    monkeypatch.setattr(
      ride_cli,
      'project_config',
      lambda _repo: SimpleNamespace(
        default_bro='bro-dev',
        harness='claude',
        summon_harness=configs.DEFAULT_SUMMON_HARNESS,
        summon_depth=5,
      ),
    )
    monkeypatch.setattr(ride_cli, 'project_root', lambda _path: Path('/repo'))
    with (
      patch('ride.cli.host_config.summon_depth', return_value=7) as resolve_depth,
      patch('ride.cli.start_session', return_value=0) as start,
    ):
      assert ride_cli.main(['ride', 'along', '--repo', '/repo', 'dev']) == 0

    resolve_depth.assert_called_once_with(5)
    assert start.call_args.args[0].summon_depth == 7

  def test_project_harness_default_is_used(self, monkeypatch):
    monkeypatch.setattr(
      ride_cli,
      'project_config',
      lambda _repo: SimpleNamespace(
        default_bro='bro-dev',
        harness='bro',
        summon_harness=configs.DEFAULT_SUMMON_HARNESS,
        summon_depth=configs.DEFAULT_SUMMON_DEPTH,
      ),
    )
    monkeypatch.setattr(ride_cli, 'project_root', lambda _path: Path('/repo'))
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--repo', '/repo', 'dev']) == 0
    assert start.call_args.args[0].harness == 'bro'

  def test_project_summon_harness_reaches_the_spec(self, monkeypatch):
    monkeypatch.setattr(
      ride_cli,
      'project_config',
      lambda _repo: SimpleNamespace(
        default_bro='bro-dev',
        harness='claude',
        summon_harness='claude',
        summon_depth=configs.DEFAULT_SUMMON_DEPTH,
      ),
    )
    monkeypatch.setattr(ride_cli, 'project_root', lambda _path: Path('/repo'))
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--repo', '/repo', 'dev']) == 0
    assert start.call_args.args[0].summon_harness == 'claude'


class TestLifecycle:
  def test_mode_parser_has_no_in_place_entry(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'solo', '--in-place', 'dev', 'prompt'])
    assert 'unrecognized arguments' in capsys.readouterr().err

  def test_resume_dispatches_scope_overrides(self):
    with patch('ride.cli.resume_session', return_value=0) as resume:
      assert (
        ride_cli.main(['ride', 'resume', '--cred', 'github+work', '--grant', '@dev', 'workspace'])
        == 0
      )
    assert resume.call_args.args == ('workspace',)
    assert resume.call_args.kwargs == {
      'cred': ['github+work'],
      'grant': ['@dev'],
      'revoke': [],
    }

  def test_scope_dispatches_harness(self):
    with patch('ride.scope_report.report_scope', return_value=0) as report:
      assert ride_cli.main(['ride', 'scope', '--bro', 'dev', '--harness', 'claude']) == 0
    assert report.call_args.kwargs == {'repo': None, 'bro': 'dev', 'harness': 'claude'}

  def test_scope_reports_a_malformed_project_config_as_a_cli_error(self, monkeypatch, capsys):
    repository = SimpleNamespace(is_url=False, git_dir=Path('/repo'))
    monkeypatch.setattr(ride_cli, '_resolve_repository_argument', lambda _value: repository)

    with (
      patch.object(
        ride_cli, 'project_config', side_effect=ValueError('malformed project configuration')
      ),
      pytest.raises(SystemExit),
    ):
      ride_cli.main(['ride', 'scope', '--repo', '/repo', '--bro', 'dev'])
    assert 'malformed project configuration' in capsys.readouterr().err

  def test_an_unusable_runtime_location_is_a_cli_error(self, monkeypatch, caplog):
    monkeypatch.setenv('XDG_DATA_HOME', 'share')
    assert ride_cli.main(['ride', 'list']) == 1
    assert 'XDG_DATA_HOME must be an absolute path' in caplog.text

  @pytest.mark.parametrize('contents', [b'{}', b'\xff'])
  def test_an_unrecognized_workspace_record_is_a_cli_error(self, caplog, contents):
    workspace = workspace_dir('old')
    workspace.mkdir(parents=True)
    (workspace / 'workspace.json').write_bytes(contents)

    assert ride_cli.main(['ride', 'list']) == 1
    assert "workspace 'old' has an unrecognised record" in caplog.text
    assert 'ride clean --force old' in caplog.text

  def test_named_noncurrent_workspace_requires_force_clean(self, caplog):
    workspace = workspace_dir('old')
    workspace.mkdir(parents=True)
    (workspace / 'unknown').write_text('state')

    assert ride_cli.main(['ride', 'clean', 'old']) == 1
    assert "workspace 'old' has an unrecognised record" in caplog.text
    assert 'ride clean --force old' in caplog.text


class TestSummonedLaunch:
  @pytest.fixture
  def pending(self, monkeypatch, tmp_path):
    record = pending_launch.PendingLaunch(
      token='TOK-1',
      runtime='/runtime',
      port=7321,
      channel_token='tk',
      type='bro',
      talk=('worker.say',),
      owner_tree=str(tmp_path / 'parent'),
      env={'IS_SANDBOX': '1'},
      extension={
        'target': 'dev',
        'prompt': 'work this out with the user',
        'may_summon': ['bro'],
        'permits': ['bro.party.start.boxed'],
        'grant': ['@bro'],
        'revoke': [':bro.party.join'],
        'summoner': {'trail_id': 'T1'},
        'repo': None,
        'into': None,
      },
    )
    pending_launch.write(record)
    return pending_bro(record)

  def test_summoned_launch_takes_prompt_and_scope_from_the_record(self, pending):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', 'dev']) == 0
    spec = start.call_args.args[0]
    assert spec.prompt == 'work this out with the user'
    assert spec.grant == ['@bro']
    assert spec.revoke == [':bro.party.join']
    assert spec.runtime_bundle == '/runtime'
    assert start.call_args.kwargs['summoned'] == pending

  def test_user_credential_pick_reaches_the_manual_child(self, pending):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', '--cred', 'github+work', 'dev'])
    assert start.call_args.args[0].cred == ['github+work']

  def test_user_credential_overrides_layer_on_the_records(self, pending):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', '--grant', 'github', 'dev'])
    assert start.call_args.args[0].grant == ['@bro', 'github']

  def test_the_partys_env_is_the_manual_childs_own(self, pending):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', 'dev'])
    assert start.call_args.args[0].env == {'IS_SANDBOX': '1'}

  def test_a_manual_child_refuses_its_own_env_additions(self, pending, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', '--env', 'EXTRA=1', 'dev'])
    assert "environment additions are the party's" in capsys.readouterr().err

  def test_a_manual_childs_session_log_layers_over_the_partys(self, pending):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', '--session-log', 'debug', 'dev'])
    assert start.call_args.args[0].env == {'IS_SANDBOX': '1', 'BRO_LOG_LEVEL': 'DEBUG'}

  def test_summoned_refuses_a_prompt(self, pending, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', 'dev', 'my own prompt'])
    assert 'takes its initial prompt from the summon request' in capsys.readouterr().err

  def test_summoned_refuses_into(self, pending, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', '--into', 'master', 'dev'])
    assert 'takes its base from the summon request' in capsys.readouterr().err

  def test_summoned_refuses_bro_overrides(self, pending, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', '--grant', '@bro', 'dev'])
    assert 'drop the @bro override(s): bro' in capsys.readouterr().err

  def test_summoned_refuses_permit_overrides(self, pending, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', '--grant', ':bro.party.join', 'dev'])
    assert 'permits were fixed by the summon request' in capsys.readouterr().err

  def test_summoned_validates_the_bro_against_the_record(self, pending, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', 'bro'])
    assert "names bro 'dev', not 'bro'" in capsys.readouterr().err

  def test_unknown_token_is_a_cli_error(self, pending, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-9', 'dev'])
    assert 'no pending manual launch for token' in capsys.readouterr().err

  def test_runtime_is_read_and_reexeced_before_the_full_record(self, pending, monkeypatch):
    path = pending_launch._path(pending.token)
    data = json.loads(path.read_text())
    data['future_field'] = {'new': 'shape'}
    path.write_text(json.dumps(data))
    calls = []

    def reexec(runtime, argv):
      calls.append((runtime, argv))
      raise RuntimeError('reexec stopped')

    monkeypatch.setattr(ride_cli, 'reexec_from_runtime', reexec)
    with pytest.raises(SystemExit):
      ride_cli.main(['old-ride', 'along', '--summoned', 'TOK-1', 'dev'])
    assert calls == [('/runtime', ['old-ride', 'along', '--summoned', 'TOK-1', 'dev'])]

  def test_summoned_solo_takes_its_prompt_from_the_record(self, pending):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'solo', '--summoned', 'TOK-1', 'dev']) == 0
    spec = start.call_args.args[0]
    assert spec.solo
    assert spec.prompt == 'work this out with the user'
    assert start.call_args.kwargs['summoned'] == pending

  def test_summoned_solo_refuses_a_positional_prompt(self, pending, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'solo', '--summoned', 'TOK-1', 'dev', 'my own prompt'])
    assert 'takes its initial prompt from the summon request' in capsys.readouterr().err

  def test_solo_without_a_summon_requires_a_prompt(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'solo', 'dev'])
    assert 'requires a prompt unless --summoned supplies it' in capsys.readouterr().err
