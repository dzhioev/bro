import contextlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import ride.cli as ride_cli
from bro.authority.authority import Atom
from bro.authority.patterns import Descriptor
from bro.base import configs, credentials, host_config
from bro.native.echo_declaration import LLMSpec as EchoLLMSpec
from bro.workspace.paths import workspace_dir
from bros.bro import Bro
from ride import pending_launch
from ride.bro_worker import pending_bro
from ride.do_ride import command as do_ride_command
from ride.workspace.metadata import Isolation


class HydrationBro(Bro):
  name = 'hydration-test'
  description = 'credential hydration route fixture'
  extra_secrets = ('github',)
  llm_spec = EchoLLMSpec()


@pytest.fixture(autouse=True)
def project(monkeypatch):
  monkeypatch.setattr(ride_cli, 'fresh_workspace_name', lambda base: f'{base}-12345678')
  monkeypatch.setattr(ride_cli, 'reexec_from_runtime', lambda _runtime, _argv: None)


def _session_command(spec) -> list[str]:
  return do_ride_command(spec)


class TestSolo:
  def test_builds_an_unattended_claude_session(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'solo', '--harness', 'claude', 'dev', 'do it']) == 0
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
    assert spec.harness is None

  def test_detached_launch_defers_policy_to_the_runtime_start(self, monkeypatch):
    monkeypatch.setattr(
      'bro.base.host_config._read', lambda: pytest.fail('pre-runtime policy read')
    )
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', 'dev']) == 0
    assert start.call_args.args[0].repo is None

  def test_detached_launch_leaves_the_summon_harness_unset(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', 'dev']) == 0
    assert start.call_args.args[0].summon_harness is None

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
        summon_harness=None,
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

  def _entry(self, llm: list[str]) -> None:
    self.config.write_text(json.dumps({'projects': {'/repo': {'bros': {'dev': {'llm': llm}}}}}))

  def test_cli_forwards_explicit_recipe_fields_for_host_normalization(self):
    self._entry(['openai:sol:xhigh'])
    with patch('ride.cli.start_session', return_value=0) as start:
      assert (
        ride_cli.main(
          ['ride', 'along', '--repo', '/repo', '--harness', 'bro', '--effort', 'low', 'dev']
        )
        == 0
      )
    spec = start.call_args.args[0]
    assert spec.llm is None
    assert spec.recipe_options == {'effort': 'low'}
    assert spec.resolved_llm == {}

  def test_cli_does_not_parse_host_policy_before_the_runtime_start(self):
    self._entry(['::ludicrous'])
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--repo', '/repo', 'dev']) == 0
    assert start.call_args.args[0].llm is None


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
      'github+flag': 'github-flag',
      'trails+host': 'trails-host',
    }.items():
      write(name, value)
    (store / credentials.STORE_FILE).write_text(
      json.dumps(
        {
          'defaults': [
            'github+default',
            'aws+default',
            'brog+default',
            'harbor+default',
          ]
        }
      )
    )

    config = tmp_path / 'bro.json'
    config.write_text(
      json.dumps(
        {
          'defaults': {'grant': ['brog', 'harbor']},
          'projects': {
            str(repository): {
              'creds': ['github+project', 'aws+project'],
              'revoke': ['brog'],
              'bros': {
                'hydration-test': {
                  'creds': ['brog+bro'],
                  'grant': ['brog', 'bro.cred=trails+host'],
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

    def launch(*scope_flags: str, harness: str | None = 'bro', mode: str = 'solo') -> int:
      result = ride_cli.main(
        [
          'ride',
          mode,
          '--unboxed',
          '--workspace',
          'hydration-route',
          '--repo',
          str(repository),
          *(['--harness', harness] if harness is not None else []),
          *scope_flags,
          'hydration-test',
          'work',
        ]
      )
      assert result is not None
      return result

    return SimpleNamespace(
      material=material, captures=captures, launch=launch, repository=repository, config=config
    )

  def test_root_launch_folds_every_layer_and_hydrates_by_selected_name(self, route):
    assert (
      route.launch(
        '--cred',
        'harbor+launch',
        '--grant',
        'github',
        '--grant',
        'bro.cred=github+flag',
        '--revoke',
        'openai',
      )
      == 0
    )

    spec, launch_scope = route.captures[-1]
    assert spec.cred == ['harbor+launch']
    assert launch_scope.scoped.required == {'github', 'aws', 'brog', 'harbor'}
    assert launch_scope.scoped.optional == set()
    assert launch_scope.mission is not None
    assert launch_scope.mission.descriptor.values['cred.github'] == 'project'
    assert launch_scope.store['creds/github+project.cred'] == b'github-project'
    assert launch_scope.store['creds/aws+project.cred'] == b'aws-project'
    assert launch_scope.store['creds/brog+bro.cred'] == b'brog-bro'
    assert launch_scope.store['creds/harbor+launch.cred'] == b'harbor-launch'
    assert 'creds/trails.cred' not in launch_scope.store
    store_config = json.loads(launch_scope.store['creds.json'])
    assert store_config['defaults'] == [
      'aws+project',
      'brog+bro',
      'github+project',
      'harbor+launch',
    ]

  def test_limit_caps_authority_without_changing_credential_hydration(self, route):
    assert (
      route.launch(
        '--cred',
        'harbor+launch',
        '--grant',
        '@reviewer',
        '--limit',
        '@reviewer for bro{placement=boxed}',
      )
      == 0
    )
    _, launch_scope = route.captures[-1]
    mission = launch_scope.mission
    assert mission is not None
    pair = mission.authority.evaluate(
      Atom('bro', 'target', 'reviewer'),
      Descriptor('bro', {}),
      mission.reading.installation.declarations,
    )
    assert pair.use.matches(Descriptor('bro', {'placement': 'boxed'}))
    assert not pair.use.matches(Descriptor('bro', {'placement': 'unboxed'}))
    assert launch_scope.store['creds/harbor+launch.cred'] == b'harbor-launch'

  def test_give_only_instance_does_not_grant_the_start_atom(self, route):
    assert (
      route.launch('--cred', 'harbor+launch', '--grant', 'webview.cred=cookies+absent to @reviewer')
      == 0
    )
    _, launch_scope = route.captures[-1]
    mission = launch_scope.mission
    assert mission is not None
    descriptor = Descriptor('bro', {'target': 'reviewer', 'placement': 'boxed'})
    types = mission.reading.installation.declarations
    assert mission.authority.evaluate(Atom('webview'), descriptor, types).use.is_empty
    pair = mission.authority.evaluate(Atom('webview', 'cred.cookies', 'absent'), descriptor, types)
    assert pair.use.is_empty
    assert pair.give.matches(descriptor)
    assert 'creds/cookies+absent.cred' not in launch_scope.store

  def test_resume_recomputes_unset_credential_picks_under_a_new_reading(self, route):
    assert route.launch('--cred', 'harbor+launch') == 0
    host = Path(host_config.HOST_CONFIG_FILE)
    configuration = json.loads(host.read_text())
    project = next(iter(configuration['projects'].values()))
    project['creds'] = ['github+resume', 'aws+project']
    host.write_text(json.dumps(configuration))
    assert ride_cli.main(['ride', 'resume', 'hydration-route']) == 0
    _, launch_scope = route.captures[-1]
    assert launch_scope.scoped.selection['github'] == 'resume'
    assert launch_scope.store['creds/github+resume.cred'] == b'github-resume'
    assert 'creds/github+project.cred' not in launch_scope.store

  def test_root_launch_fails_for_a_picked_absent_instance(self, route, caplog):
    assert route.launch('--cred', 'harbor+absent') == 1

    assert route.captures == []
    assert 'required credential instances are missing' in caplog.text
    assert 'harbor' in caplog.text

  def test_root_authority_may_name_an_absent_instance(self, route):
    assert route.launch('--cred', 'harbor+launch', '--grant', 'bro.cred=github+absent') == 0
    _, launch_scope = route.captures[-1]
    assert 'creds/github+absent.cred' not in launch_scope.store
    assert launch_scope.mission is not None

  def test_old_launch_grammar_is_refused_before_workspace_creation(self, route, caplog):
    assert route.launch('--grant', ':launch.bro.pass.github') == 1
    assert route.captures == []
    assert 'invalid name' in caplog.text

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
          '--grant',
          'bro.cred=github+resume',
          'hydration-route',
        ]
      )
      == 0
    )

    spec, launch_scope = route.captures[-1]
    assert spec.cred == ['github+resume']
    assert spec.mission.descriptor.values['cred.harbor'] == 'launch'
    assert spec.revoke == ['aws']
    assert launch_scope.scoped.required == {'github', 'brog', 'harbor'}
    assert launch_scope.mission.descriptor.values['cred.github'] == 'resume'
    assert launch_scope.store['creds/github+resume.cred'] == b'github-resume'
    assert 'creds/aws+project.cred' not in launch_scope.store

  def test_summoned_child_pass_flags_resume_as_a_root_launch(self, route):
    assert (
      route.launch(
        '--cred',
        'harbor+launch',
        '--cred',
        'github+flag',
        '--grant',
        'github',
      )
      == 0
    )
    route.captures.clear()

    assert ride_cli.main(['ride', 'resume', 'hydration-route']) == 0

    spec, launch_scope = route.captures[-1]
    assert spec.cred == []
    assert spec.grant == []
    assert spec.mission.descriptor.values['cred.harbor'] == 'launch'
    assert spec.mission.descriptor.values['cred.github'] == 'flag'
    assert launch_scope.store['creds/github+flag.cred'] == b'github-flag'
    assert launch_scope.hydrated_kinds == {'github', 'aws', 'brog', 'harbor'}

  def test_resume_may_grant_authority_over_an_absent_instance(self, route):
    assert route.launch('--cred', 'harbor+launch') == 0
    route.captures.clear()
    assert (
      ride_cli.main(['ride', 'resume', '--grant', 'bro.cred=github+absent', 'hydration-route']) == 0
    )
    assert route.captures[-1][1].mission is not None
    assert 'creds/github+absent.cred' not in route.captures[-1][1].store

  @pytest.mark.parametrize('mode', ['solo', 'along'])
  @pytest.mark.parametrize(
    ('selected', 'project', 'host'),
    [
      ('bro', 'not-installed', 'not-installed'),
      (None, 'bro', 'not-installed'),
      (None, None, 'bro'),
    ],
    ids=['launch', 'project', 'host'],
  )
  def test_harness_precedence_reaches_the_started_session(
    self, route, mode, selected, project, host
  ):
    configuration = json.loads(route.config.read_text())
    configuration['harness'] = host
    route.config.write_text(json.dumps(configuration))
    if project is not None:
      with (route.repository / 'pyproject.toml').open('a') as output:
        output.write(f'harness = "{project}"\n')
    assert route.launch('--cred', 'harbor+launch', harness=selected, mode=mode) == 0
    spec, scope = route.captures[-1]
    assert spec.harness == 'bro'
    assert scope.mission.descriptor.values['harness'] == spec.harness

  @pytest.mark.parametrize('mode', ['solo', 'along'])
  def test_missing_harness_refuses_before_a_workspace(self, route, mode, caplog):
    assert route.launch(harness=None, mode=mode) == 1
    assert route.captures == []
    assert not (workspace_dir('hydration-route') / 'workspace.json').exists()
    assert (
      'no harness: pass --harness, set [tool.bro] harness, or set "harness" in ~/.bro.json'
      in caplog.text
    )

  @pytest.mark.parametrize('layer', ['launch', 'project', 'host'])
  def test_uninstalled_harness_is_refused_without_falling_back(self, route, layer, caplog):
    configuration = json.loads(route.config.read_text())
    configuration['harness'] = 'not-installed' if layer == 'host' else 'bro'
    route.config.write_text(json.dumps(configuration))
    if layer == 'project':
      with (route.repository / 'pyproject.toml').open('a') as output:
        output.write('harness = "not-installed"\n')
    assert route.launch(harness='not-installed' if layer == 'launch' else None) == 1
    assert route.captures == []
    assert "harness 'not-installed' is not installed" in caplog.text

  def test_resume_keeps_the_harness_after_project_and_host_changes(self, route):
    assert route.launch('--cred', 'harbor+launch') == 0
    configuration = json.loads(route.config.read_text())
    configuration['harness'] = 'not-installed'
    configuration['summon-harness'] = 'not-installed'
    route.config.write_text(json.dumps(configuration))
    with (route.repository / 'pyproject.toml').open('a') as output:
      output.write('harness = "not-installed"\nsummon-harness = "not-installed"\n')
    assert ride_cli.main(['ride', 'resume', 'hydration-route']) == 0
    assert route.captures[-1][0].harness == 'bro'

  @pytest.mark.parametrize('layer', ['launch', 'project', 'host', 'absent'])
  def test_scope_resolves_the_same_harness_layers(self, route, layer, capsys):
    if layer == 'project':
      with (route.repository / 'pyproject.toml').open('a') as output:
        output.write('harness = "bro"\n')
    configuration = json.loads(route.config.read_text())
    if layer in ('project', 'host'):
      configuration['harness'] = 'bro' if layer == 'host' else 'not-installed'
    route.config.write_text(json.dumps(configuration))
    flags = ['--harness', 'bro'] if layer == 'launch' else []
    result = ride_cli.main(
      ['ride', 'scope', '--repo', str(route.repository), '--bro', 'hydration-test', *flags]
    )
    output = capsys.readouterr().out
    if layer == 'absent':
      assert result == 1
      assert (
        'no harness: pass --harness, set [tool.bro] harness, or set "harness" in ~/.bro.json'
        in output
      )
    else:
      assert result == 0
      descriptor = json.loads(output.splitlines()[0].removeprefix('descriptor: '))
      assert descriptor['harness'] == 'bro'


class TestAlong:
  def test_builds_an_attended_claude_session(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--harness', 'claude', 'dev', 'do it']) == 0
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

  def test_cli_defers_provider_compatibility_to_normalization(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--provider', 'openai', 'dev']) == 0
    assert start.call_args.args[0].recipe_options == {'provider': 'openai'}

  def test_cli_defers_target_existence_to_the_authorized_start(self):
    for harness in ('claude', 'bro'):
      with patch('ride.cli.start_session', return_value=0) as start:
        assert ride_cli.main(['ride', 'along', '--harness', harness, 'no-such-bro']) == 0
      assert start.call_args.args[0].bro == 'no-such-bro'

  def test_bro_harness_builds_a_native_chat(self):
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--harness', 'bro', 'dev']) == 0
    spec = start.call_args.args[0]
    assert spec.harness == 'bro'
    assert _session_command(spec) == [
      'do-ride', 'along', '--workspace', 'ride-dev-12345678', '--harness', 'bro',
      '--hold', 'attended', '--', 'dev',
    ]  # fmt: skip

  def test_attached_launch_defers_project_defaults_to_the_runtime_start(self, monkeypatch):
    monkeypatch.setattr(ride_cli, 'project_root', lambda _path: Path('/repo'))
    monkeypatch.setattr(
      'bro.workspace.project.project_config', lambda *args: pytest.fail('pre-runtime project read')
    )
    monkeypatch.setattr('bro.base.host_config._read', lambda: pytest.fail('pre-runtime host read'))
    with patch('ride.cli.start_session', return_value=0) as start:
      assert ride_cli.main(['ride', 'along', '--repo', '/repo', 'dev']) == 0
    spec = start.call_args.args[0]
    assert spec.repo == '/repo'
    assert not spec.harness_set
    assert '--harness' not in spec.to_command_argv()


class TestLifecycle:
  def test_mode_parser_has_no_in_place_entry(self, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'solo', '--in-place', 'dev', 'prompt'])
    assert 'unrecognized arguments' in capsys.readouterr().err

  @pytest.mark.parametrize(
    'launch',
    [
      lambda tail: ride_cli.main(['ride', 'solo', '--runtime-bundle', '/runtime', 'dev', *tail]),
      lambda tail: ride_cli.main(['ride', 'along', '--runtime-bundle', '/runtime', 'dev', *tail]),
      lambda tail: ride_cli.alias_main(
        ['ask', '--runtime-bundle', '/runtime', 'dev', *tail], solo=True
      ),
    ],
    ids=['solo', 'along', 'alias'],
  )
  def test_a_session_launch_refuses_harness_arguments_before_any_workspace(self, capsys, launch):
    with (
      patch.object(ride_cli, 'reexec_from_runtime') as reexec,
      patch.object(ride_cli, 'start_session') as start,
      pytest.raises(SystemExit),
    ):
      launch(['hello', '--', '--debug', 'mcp'])
    assert 'no harness arguments after `--`; drop `-- --debug mcp`' in capsys.readouterr().err
    assert reexec.call_count == 0 and start.call_count == 0

  def test_resume_dispatches_scope_overrides(self):
    with patch('ride.cli.resume_session', return_value=0) as resume:
      assert (
        ride_cli.main(
          [
            'ride',
            'resume',
            '--cred',
            'github+work',
            '--grant',
            '@dev',
            '--limit',
            '@dev for *',
            'workspace',
          ]
        )
        == 0
      )
    assert resume.call_args.args == ('workspace',)
    assert resume.call_args.kwargs == {
      'cred': ['github+work'],
      'grant': ['@dev'],
      'revoke': [],
      'limit': ['@dev for *'],
    }

  def test_check_harness_starts_its_runtime_probe(self):
    driver = MagicMock()
    with patch.object(ride_cli, 'get_harness', return_value=driver) as resolve:
      assert ride_cli.main(['ride', 'check-harness', 'claude']) is None
    resolve.assert_called_once_with('claude')
    driver.check_runtime.assert_called_once_with()

  def test_scope_dispatches_harness(self):
    with patch('ride.scope_report.report_scope', return_value=0) as report:
      assert ride_cli.main(['ride', 'scope', '--bro', 'dev', '--harness', 'claude']) == 0
    assert report.call_args.kwargs == {
      'repo': None,
      'bro': 'dev',
      'harness': 'claude',
      'llm': None,
      'recipe_options': {},
    }

  def test_scope_reports_a_malformed_project_config(self, tmp_path, capsys):
    (tmp_path / 'pyproject.toml').write_text('[tool.bro]\nharness = 5\n')
    with patch.object(ride_cli, '_resolve_repository_argument') as resolve:
      from ride.repository import Repository

      resolve.return_value = Repository(str(tmp_path), tmp_path)
      assert ride_cli.main(['ride', 'scope', '--repo', str(tmp_path), '--bro', 'dev']) == 1
    assert 'harness' in capsys.readouterr().out

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
        'request': {'type': 'bro', 'values': {'target': 'dev', 'cred.github': 'passed'}},
        'root': 'parent-root',
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
    assert spec.cred == []
    assert spec.grant == []
    assert spec.revoke == []
    assert pending.request['values']['cred.github'] == 'passed'
    assert spec.runtime_bundle == '/runtime'
    assert start.call_args.kwargs['summoned'] == pending

  def test_user_credential_pick_reaches_the_manual_child(self, pending):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', '--cred', 'github+work', 'dev'])
    assert start.call_args.args[0].cred == ['github+work']

  def test_user_credential_overrides_layer_on_the_records(self, pending):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', '--grant', 'github', 'dev'])
    assert start.call_args.args[0].grant == ['github']

  def test_user_credential_revoke_drops_the_passed_kind_and_pick(self, pending):
    with patch('ride.cli.start_session', return_value=0) as start:
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', '--revoke', 'github', 'dev'])
    spec = start.call_args.args[0]
    assert spec.cred == []
    assert spec.grant == []
    assert spec.revoke == ['github']

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
    assert 'authority entries belong to the prepared request' in capsys.readouterr().err

  def test_summoned_names_a_retired_launch_override_without_a_traceback(self, pending, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(['ride', 'along', '--summoned', 'TOK-1', '--grant', ':bro.party.join', 'dev'])
    assert "invalid name ':bro'" in capsys.readouterr().err

  def test_summoned_refuses_launch_overrides(self, pending, capsys):
    with pytest.raises(SystemExit):
      ride_cli.main(
        ['ride', 'along', '--summoned', 'TOK-1', '--grant', 'bro.placement=join', 'dev']
      )
    assert 'authority entries belong to the prepared request' in capsys.readouterr().err

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


@pytest.mark.parametrize(
  'flags, llm, options',
  [
    (['--llm', 'uninstalled:future:high'], 'uninstalled:future:high', {}),
    (
      ['--provider', 'uninstalled', '--model', 'future', '--effort', 'high', '--fast'],
      None,
      {'provider': 'uninstalled', 'model': 'future', 'effort': 'high', 'fast': True},
    ),
  ],
)
def test_scope_forwards_llm_flags_to_the_prospective_reading(flags, llm, options):
  with patch('ride.scope_report.report_scope', return_value=0) as report:
    assert ride_cli.main(['ride', 'scope', '--bro', 'dev', '--harness', 'bro', *flags]) == 0
  assert report.call_args.kwargs['llm'] == llm
  assert report.call_args.kwargs['recipe_options'] == options


def test_scope_llm_excludes_piece_flags():
  with pytest.raises(SystemExit):
    ride_cli.main(['ride', 'scope', '--bro', 'dev', '--llm', 'echo', '--fast'])
