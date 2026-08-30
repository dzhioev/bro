import subprocess
import sys

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
def invocations(monkeypatch):
  """the argument lists a stage hands to `run`, in place of running anything."""
  calls: list[tuple[str, ...]] = []
  monkeypatch.setattr(run_tests, 'run', lambda *args, **kwargs: calls.append(args))
  return calls


@pytest.fixture
def repository(monkeypatch, tmp_path):
  """a checkout the whole --changed deduction runs over: a roster covering both
  halves of its rule, and a nested project only part of the tree reaches."""

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
  write('thing/heavy.py')
  write('thing/heavy_test.py', 'from thing import heavy\n')
  write(f'{run_tests.BENCHMARK}/pyproject.toml', '[project]\nname = "probe-benchmark"\n')
  write(f'{run_tests.BENCHMARK}/bro/benchmark/job.py', 'from thing import store\n')
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
  monkeypatch.setattr(run_tests, 'SINGLE_PROCESS_PYTEST_FILES', ['thing/heavy_test.py'])
  monkeypatch.setattr(
    run_tests,
    'DISTRIBUTIONS',
    [
      run_tests.Distribution(directory=directory, deptry_exclude=(), deptry_known_first_party=())
      for directory in ('.', run_tests.BENCHMARK)
    ],
  )
  return tmp_path


def test_a_change_selects_the_tests_that_reach_it(repository):
  (repository / 'thing/store.py').write_text('CHANGED = 1\n')

  assert run_tests.select('main').roster == [
    'thing/store_test.py',
    'thing/api_test.py',
    'thing/policy_test.py',
  ]


def test_the_single_process_roster_is_narrowed_the_same_way(repository):
  (repository / 'thing/heavy.py').write_text('CHANGED = 1\n')

  selection = run_tests.select('main')

  assert selection.single_process_roster == ['thing/heavy_test.py']
  assert selection.roster == ['thing/policy_test.py']


def test_the_single_process_roster_runs_outside_the_worker_pool(invocations):
  run_tests.unit_stage(['thing/store_test.py'], ['thing/heavy_test.py'])

  assert invocations == [
    (sys.executable, '-m', 'pytest', '-n', 'auto', 'thing/store_test.py'),
    (sys.executable, '-m', 'pytest', 'thing/heavy_test.py'),
  ]


def test_an_empty_selection_runs_no_pytest_at_all(invocations):
  # a bare `pytest` collects the whole tree, so an empty list is no argument list
  run_tests.unit_stage([], [])

  assert invocations == []


def test_a_test_module_with_no_source_module_of_its_own_always_runs(repository):
  (repository / 'thing/elsewhere.py').write_text('CHANGED = 1\n')

  selected = run_tests.select('main').roster

  assert 'thing/policy_test.py' in selected
  assert 'thing/store_test.py' not in selected


def test_lint_covers_the_innermost_project_holding_the_change(repository):
  (repository / f'{run_tests.BENCHMARK}/bro/benchmark/job.py').write_text('CHANGED = 1\n')

  selection = run_tests.select('main')

  assert [distribution.directory for distribution in selection.distributions] == [
    run_tests.BENCHMARK
  ]


def test_lint_covers_the_root_for_a_change_no_nested_project_holds(repository):
  (repository / 'thing/store.py').write_text('CHANGED = 1\n')

  selection = run_tests.select('main')

  assert [distribution.directory for distribution in selection.distributions] == ['.']


def test_a_change_the_benchmark_project_imports_keeps_its_stage(repository):
  (repository / 'thing/store.py').write_text('CHANGED = 1\n')

  assert run_tests.select('main').dropped == frozenset()


def test_a_change_the_benchmark_project_cannot_reach_drops_its_stage(repository):
  (repository / 'thing/elsewhere.py').write_text('CHANGED = 1\n')

  assert run_tests.select('main').dropped == frozenset({'benchmark'})


def test_a_project_metadata_change_keeps_the_benchmark_stage(repository):
  (repository / 'pyproject.toml').write_text('[tool.uv.workspace]\nmembers = ["member", "other"]\n')

  assert run_tests.select('main').dropped == frozenset()


def test_a_dropped_stage_reads_skipped_in_the_verdict(monkeypatch, capsys):
  ran = []
  monkeypatch.setattr(
    run_tests,
    'STAGES',
    [
      run_tests.Stage('types', lambda: ran.append('types')),
      run_tests.Stage('benchmark', lambda: ran.append('benchmark')),
    ],
  )
  monkeypatch.setattr(
    run_tests,
    'select',
    lambda base: run_tests.Selection(
      roster=(), single_process_roster=(), distributions=(), dropped=frozenset({'benchmark'})
    ),
  )

  assert run_tests.main(['run-tests', '--changed']) is None
  assert ran == ['types']
  assert 'gate: types ok | benchmark skipped' in capsys.readouterr().err


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
