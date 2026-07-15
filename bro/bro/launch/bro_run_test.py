import bro_run
from cw.constants import bro_git_identity_env


def test_describe_composes_the_host_pinned_command():
  launch = bro_run.describe('ppp-dev', ['hi', '--slow'], cli_name='call')
  assert launch.command == ['call', 'ppp-dev', 'hi', '--slow', '--host']


def test_describe_env_carries_identity_and_bro():
  launch = bro_run.describe('ppp-dev', ['hi'])
  assert launch.env == {'CW_BRO': 'ppp-dev', **bro_git_identity_env()}


def test_describe_scopes_to_the_bro():
  launch = bro_run.describe('ppp-dev', ['hi'])
  # ppp-dev's manifest (github + brog) + its llm key + the mandatory trails sink
  assert {'github', 'brog', 'trails'} <= launch.secrets
  # ppp-dev doesn't deploy → no docker socket
  assert launch.docker_sock is False


def test_describe_base_ref_rides_cw_base_ref():
  launch = bro_run.describe('ppp-dev', ['hi'], base_ref='REF-SHA')
  assert launch.env['CW_BASE_REF'] == 'REF-SHA'


def test_describe_no_trails_drops_secret_and_disables_recording():
  launch = bro_run.describe('ppp-dev', ['hi'], trails=False)
  assert 'trails' not in launch.secrets
  assert launch.env['TRAILS_DISABLED'] == '1'


def _workspace_dirs(monkeypatch, tmp_path):
  monkeypatch.setattr(bro_run, '_project_root', lambda: tmp_path)
  worktrees = tmp_path / 'var' / 'cw' / 'worktrees'
  containers = tmp_path / 'var' / 'cw' / 'containers'
  worktrees.mkdir(parents=True)
  containers.mkdir(parents=True)
  return worktrees, containers


def test_fresh_workspace_name_is_unique_per_call(monkeypatch, tmp_path):
  _workspace_dirs(monkeypatch, tmp_path)
  first = bro_run.fresh_workspace_name('ask-ppp-dev')
  second = bro_run.fresh_workspace_name('ask-ppp-dev')
  assert first.startswith('ask-ppp-dev-')
  assert first != second


def test_fresh_workspace_name_regenerates_on_worktree_collision(monkeypatch, tmp_path):
  worktrees, _ = _workspace_dirs(monkeypatch, tmp_path)
  suffixes = iter(['aaaaaa', 'bbbbbb'])
  monkeypatch.setattr(bro_run.secrets, 'token_hex', lambda _: next(suffixes))
  (worktrees / 'idea-aaaaaa').mkdir()
  assert bro_run.fresh_workspace_name('idea') == 'idea-bbbbbb'


def test_fresh_workspace_name_regenerates_on_container_collision(monkeypatch, tmp_path):
  _, containers = _workspace_dirs(monkeypatch, tmp_path)
  suffixes = iter(['aaaaaa', 'bbbbbb'])
  monkeypatch.setattr(bro_run.secrets, 'token_hex', lambda _: next(suffixes))
  (containers / 'idea-aaaaaa').mkdir()
  assert bro_run.fresh_workspace_name('idea') == 'idea-bbbbbb'
