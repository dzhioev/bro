import subprocess

import pytest
import yaml

from bro.local import run_tests


def test_benchmark_stage_refuses_to_rewrite_a_stale_lock(monkeypatch, tmp_path):
  directory = tmp_path / run_tests.BENCHMARK
  directory.mkdir()
  project_file = directory / 'pyproject.toml'
  project_file.write_text(
    """
[project]
name = "lock-probe"
version = "0.1.0"
requires-python = ">=3.12"

[tool.uv]
package = false
""".lstrip()
  )
  subprocess.run(('uv', 'lock'), check=True, cwd=directory, capture_output=True)
  lock_file = directory / 'uv.lock'
  locked = lock_file.read_bytes()
  project_file.write_text(project_file.read_text().replace('0.1.0', '0.2.0'))
  monkeypatch.setattr(run_tests, 'DIR', tmp_path)

  with pytest.raises(subprocess.CalledProcessError) as raised:
    run_tests.benchmark_stage()

  assert raised.value.cmd == ('uv', 'sync', '--locked', '--all-groups')
  assert lock_file.read_bytes() == locked


def test_the_workflow_matrix_names_every_gate_stage():
  workflow = yaml.safe_load((run_tests.DIR / '.github/workflows/tests.yml').read_text())

  matrix = workflow['jobs']['stage']['strategy']['matrix']['stage']

  assert matrix == [stage.name for stage in run_tests.STAGES]


@pytest.fixture
def repository(monkeypatch, tmp_path):
  """a checkout whose roster covers both halves of the --changed rule."""

  def write(path: str, text: str = '') -> None:
    full = tmp_path / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(text)

  write('pyproject.toml', '[tool.uv.workspace]\nmembers = ["member"]\n')
  write('thing/__init__.py')
  write('thing/store.py')
  write('thing/store_test.py', 'from thing import store\n')
  write('thing/api.py', 'from thing import store\n')
  write('thing/api_test.py', 'from thing import api\n')
  write('thing/elsewhere.py')
  write('thing/elsewhere_test.py', 'from thing import elsewhere\n')
  write('thing/policy_test.py', 'import json\n')
  for command in (
    ('git', 'init', '-b', 'main'),
    ('git', 'config', 'user.email', 'gate@example.com'),
    ('git', 'config', 'user.name', 'gate'),
    ('git', 'add', '.'),
    ('git', 'commit', '-m', 'base'),
  ):
    subprocess.run(command, cwd=tmp_path, check=True, capture_output=True)
  monkeypatch.setattr(run_tests, 'DIR', tmp_path)
  monkeypatch.setattr(
    run_tests,
    'PYTEST_FILES',
    [
      'thing/store_test.py',
      'thing/api_test.py',
      'thing/elsewhere_test.py',
      'thing/policy_test.py',
    ],
  )
  return tmp_path


def test_a_change_selects_the_tests_that_reach_it(repository):
  (repository / 'thing/store.py').write_text('CHANGED = 1\n')

  assert run_tests.reachable_roster('main') == [
    'thing/store_test.py',
    'thing/api_test.py',
    'thing/policy_test.py',
  ]


def test_a_test_module_with_no_source_module_of_its_own_always_runs(repository):
  (repository / 'thing/elsewhere.py').write_text('CHANGED = 1\n')

  selected = run_tests.reachable_roster('main')

  assert 'thing/policy_test.py' in selected
  assert 'thing/store_test.py' not in selected


def test_every_selected_stage_runs_and_the_summary_reports_each(monkeypatch, capsys):
  def fail() -> None:
    raise subprocess.CalledProcessError(1, ('probe',))

  ran = []
  monkeypatch.setattr(
    run_tests,
    'STAGES',
    [
      run_tests.Stage('first', fail),
      run_tests.Stage('second', lambda: ran.append('second')),
    ],
  )

  assert run_tests.main([]) == 1
  assert ran == ['second']
  assert 'gate: first FAILED | second ok' in capsys.readouterr().err
