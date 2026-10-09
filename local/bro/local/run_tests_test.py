import io
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from bro.dev.packaging_policy import TEST_MODULE_NAMES, TEST_MODULE_SUFFIXES
from bro.local import gate_display, green_trees, run_tests

CLEAN_TREE = green_trees.clean_tree


@pytest.fixture(autouse=True)
def display(monkeypatch):
  """a stage under way on a display writing where no test reads."""
  shown = gate_display.Plain(io.StringIO(), color=False, verbose=False)
  shown.stage_started('probe', 'probe')
  monkeypatch.setattr(run_tests, '_display', shown)
  return shown


@pytest.fixture(autouse=True)
def no_tree_notes(monkeypatch):
  """the gate as over a worktree with edits, so no run notes anything on this checkout's tree."""
  monkeypatch.setattr(green_trees, 'clean_tree', lambda root: None)


def test_a_failed_command_carries_its_whole_output():
  with pytest.raises(subprocess.CalledProcessError) as raised:
    run_tests.run(
      sys.executable,
      '-c',
      'import sys; print("first", flush=True); print("second", file=sys.stderr); sys.exit(3)',
    )

  assert raised.value.returncode == 3
  assert raised.value.output == 'first\nsecond\n'


def test_a_command_output_reaches_the_display_line_by_line(monkeypatch):
  stream = io.StringIO()
  monkeypatch.setattr(run_tests, '_display', gate_display.Plain(stream, color=False, verbose=True))

  run_tests.run(sys.executable, '-c', 'print("one"); print("two")')

  assert stream.getvalue() == 'one\ntwo\n'


def test_a_command_reads_the_display_color_decision_from_its_environment(monkeypatch):
  stream = io.StringIO()
  shown = gate_display.Plain(stream, color=False, verbose=True)
  monkeypatch.setattr(run_tests, '_display', shown)
  monkeypatch.setenv('FORCE_COLOR', '1')
  probe = 'import os; print(os.environ.get("FORCE_COLOR"), os.environ.get("NO_COLOR"))'

  run_tests.run(sys.executable, '-c', probe)
  shown.color = True
  run_tests.run(sys.executable, '-c', probe)

  assert stream.getvalue() == 'None 1\n1 None\n'


@pytest.mark.parametrize('directory', ['', 'package/nested/'])
def test_deptry_skips_every_test_module_the_packaging_policy_names(directory):
  test_modules = [
    *(f'{directory}{name}.py' for name in TEST_MODULE_NAMES),
    *(f'{directory}subject{suffix}.py' for suffix in TEST_MODULE_SUFFIXES),
  ]
  shipped = [f'{directory}subject.py', f'{directory}sub{TEST_MODULE_NAMES[0]}.py']

  assert [path for path in test_modules if not re.match(run_tests.TEST_MODULE_PATTERN, path)] == []
  assert [path for path in shipped if re.match(run_tests.TEST_MODULE_PATTERN, path)] == []


def test_the_commands_deaf_to_the_environment_are_told_the_color_decision_by_flag(
  invocations, display
):
  distribution = run_tests.Distribution(
    directory='.', deptry_exclude=(), deptry_known_first_party=()
  )

  run_tests.lint_stage([distribution])
  assert invocations[1] == (sys.executable, '-m', 'deptry', '.', '--no-ansi')
  assert Path(invocations[-1][0]).name == 'shellcheck'
  assert '--color=always' not in invocations[-1]

  display.color = True
  invocations.clear()
  run_tests.lint_stage([distribution])
  assert invocations[1] == (sys.executable, '-m', 'deptry', '.')
  assert invocations[-1][:2] == (str(Path(sys.executable).parent / 'shellcheck'), '--color=always')


def test_a_project_stage_refuses_to_rewrite_a_stale_lock(monkeypatch, tmp_path):
  project = run_tests.Project(stage='probe', directory='probe', pytest_files=[])
  directory = tmp_path / project.directory
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
    run_tests.project_stage(project)

  assert raised.value.cmd == ('uv', 'sync', '-q', '--locked', '--all-groups')
  assert '--locked' in raised.value.output
  assert lock_file.read_bytes() == locked


def test_the_workflow_matrix_names_every_default_gate_stage():
  workflow = yaml.safe_load((run_tests.DIR / '.github/workflows/tests.yml').read_text())

  matrix = workflow['jobs']['stage']['strategy']['matrix']
  named = [*matrix['stage'], *(entry['stage'] for entry in matrix['include'])]

  assert sorted(set(named)) == sorted(stage.name for stage in run_tests.STAGES if not stage.opt_in)


def test_the_workflow_matrix_deals_every_broker_e2e_shard():
  workflow = yaml.safe_load((run_tests.DIR / '.github/workflows/tests.yml').read_text())

  shards = [
    entry['shard']
    for entry in workflow['jobs']['stage']['strategy']['matrix']['include']
    if entry['stage'] == 'broker_e2e'
  ]

  count = len(shards)
  assert shards == [f'{index}/{count}' for index in range(1, count + 1)]


def test_a_shard_reaches_the_broker_e2e_stage_alone(monkeypatch, invocations):
  monkeypatch.setattr(
    run_tests, 'STAGES', [run_tests.Stage('broker_e2e', run_tests.broker_e2e_stage, 'a probe')]
  )

  assert run_tests.main(['run-tests', '--only', 'broker_e2e', '--shard', '2/3']) is None
  assert invocations == [
    (sys.executable, '-m', 'pytest', '-q', run_tests.BROKER_E2E_PYTEST_FILE, '--shard=2/3')
  ]


@pytest.mark.parametrize(
  'argv',
  [
    ['--shard', '1/3'],
    ['--only', 'docker', '--shard', '1/3'],
    ['--only', 'broker_e2e', '--only', 'docker', '--shard', '1/3'],
    ['--only', 'broker_e2e', '--shard', '4/3'],
  ],
)
def test_a_shard_outside_the_broker_e2e_stage_or_its_count_is_refused(argv, capsys):
  with pytest.raises(SystemExit) as raised:
    run_tests.main(['run-tests', *argv])

  assert raised.value.code == 2
  assert 'shard' in capsys.readouterr().err


def test_the_live_broker_stages_name_their_own_modules(invocations):
  run_tests.broker_e2e_stage()
  run_tests.webview_e2e_stage()

  assert invocations == [
    (sys.executable, '-m', 'pytest', '-q', run_tests.BROKER_E2E_PYTEST_FILE),
    (sys.executable, '-m', 'pytest', '-q', *run_tests.WEBVIEW_E2E_PYTEST_FILES),
  ]


def test_the_token_spending_stages_name_each_probe_on_the_command_line(invocations):
  run_tests.llm_stage()
  run_tests.conformance_stage()

  assert invocations == [
    (sys.executable, '-m', 'pytest', '-q', *run_tests.LLM_PYTEST_FILES),
    (sys.executable, '-m', 'pytest', '-q', *run_tests.CONFORMANCE_PYTEST_FILES),
  ]


@pytest.mark.parametrize(
  ('probe', 'roster'),
  [
    pytest.param('dev/bros/dev/commit_llm_test.py', 'LLM_PYTEST_FILES', id='llm'),
    pytest.param(
      'local/bro/local/watch_wait_conformance_test.py',
      'CONFORMANCE_PYTEST_FILES',
      id='conformance',
    ),
  ],
)
def test_a_probe_is_collected_only_as_a_named_file(probe, roster):
  def collected(path: str) -> str:
    return subprocess.run(
      [sys.executable, '-m', 'pytest', '--collect-only', '-q', '-p', 'no:cacheprovider', path],
      cwd=run_tests.DIR,
      check=True,
      capture_output=True,
      text=True,
    ).stdout

  assert probe in getattr(run_tests, roster)

  assert probe not in collected(str(Path(probe).parent))
  assert probe in collected(probe)


def test_an_opt_in_stage_runs_only_when_named(monkeypatch, capsys):
  ran = []
  monkeypatch.setattr(
    run_tests,
    'STAGES',
    [
      run_tests.Stage('types', lambda: ran.append('types'), 'a probe'),
      run_tests.Stage('llm', lambda: ran.append('llm'), 'a probe', opt_in=True),
    ],
  )

  assert run_tests.main([]) is None
  assert ran == ['types']
  assert capsys.readouterr().err.endswith('gate: types ok\n')

  assert run_tests.main(['run-tests', '--only', 'llm']) is None
  assert ran == ['types', 'llm']


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
  monkeypatch.setattr(
    run_tests,
    'PROJECTS',
    [run_tests.Project(stage='benchmark', directory=run_tests.BENCHMARK, pytest_files=[])],
  )
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


def test_an_empty_selection_runs_no_pytest_at_all(invocations):
  # a bare `pytest` collects the whole tree, so an empty list is no argument list
  run_tests.unit_stage([])

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


def test_the_lint_stage_refuses_a_file_the_formatter_would_rewrite(monkeypatch, tmp_path):
  # the file lints clean, so the format check is the only step that can refuse it
  (tmp_path / 'drifted.py').write_text('VALUE = {  "a": 1 }\n')
  monkeypatch.setattr(run_tests, 'DIR', tmp_path)

  with pytest.raises(subprocess.CalledProcessError) as raised:
    run_tests.lint_stage([])

  assert raised.value.cmd == (sys.executable, '-m', 'ruff', 'format', '--check', '.')


def test_the_lint_stage_refuses_a_shell_script_shellcheck_flags(monkeypatch, tmp_path):
  subprocess.run(('git', 'init', '-q'), check=True, cwd=tmp_path)
  # an unquoted expansion: nothing ruff reads, so shellcheck is the only step that can refuse it
  (tmp_path / 'probe.sh').write_text('#!/usr/bin/env bash\necho $1\n')
  monkeypatch.setattr(run_tests, 'DIR', tmp_path)

  with pytest.raises(subprocess.CalledProcessError) as raised:
    run_tests.lint_stage([])

  assert Path(raised.value.cmd[0]).name == 'shellcheck'
  assert raised.value.cmd[1:] == ('probe.sh',)


def test_a_dropped_stage_reads_skipped_in_the_verdict(monkeypatch, capsys):
  ran = []
  monkeypatch.setattr(
    run_tests,
    'STAGES',
    [
      run_tests.Stage('types', lambda: ran.append('types'), 'a probe'),
      run_tests.Stage('benchmark', lambda: ran.append('benchmark'), 'a probe'),
    ],
  )
  monkeypatch.setattr(
    run_tests,
    'select',
    lambda base: run_tests.Selection(roster=(), distributions=(), dropped=frozenset({'benchmark'})),
  )

  assert run_tests.main(['run-tests', '--changed']) is None
  assert ran == ['types']
  assert 'gate: types ok | benchmark skipped' in capsys.readouterr().err


def test_every_selected_stage_runs_and_each_failed_command_is_replayed_whole(monkeypatch, capsys):
  def fail() -> None:
    run_tests.step('probe')
    raise subprocess.CalledProcessError(1, ('probe',), output='first line\nlast line\n')

  ran = []
  monkeypatch.setattr(
    run_tests,
    'STAGES',
    [
      run_tests.Stage('first', fail, 'a probe that fails'),
      run_tests.Stage('second', lambda: ran.append('second'), 'a probe that passes'),
    ],
  )

  assert run_tests.main([]) == 1
  assert ran == ['second']
  written = capsys.readouterr().err
  assert '=== first: probe exited with 1 ===\nfirst line\nlast line\n' in written
  assert written.endswith('gate: first FAILED | second ok\n')


@pytest.fixture
def rosters(repository, monkeypatch):
  """the checkout above with every roster over its own test modules: the two it
  sets, a broker e2e module at the root, and a benchmark module named from the
  benchmark project."""
  (repository / 'thing/e2e_test.py').write_text('')
  (repository / f'{run_tests.BENCHMARK}/bro/benchmark/job_test.py').write_text('')
  monkeypatch.setattr(run_tests, 'BROKER_E2E_PYTEST_FILE', 'thing/e2e_test.py')
  monkeypatch.setattr(
    run_tests,
    'PROJECTS',
    [
      run_tests.Project(
        stage='benchmark', directory=run_tests.BENCHMARK, pytest_files=['bro/benchmark/job_test.py']
      )
    ],
  )
  for name in (
    'DOCKER_PYTEST_FILES',
    'WEBVIEW_E2E_PYTEST_FILES',
    'LLM_PYTEST_FILES',
    'CONFORMANCE_PYTEST_FILES',
    'BENCHMARK_E2E_PYTEST_FILES',
  ):
    monkeypatch.setattr(run_tests, name, [])
  return repository


def test_rosters_naming_every_test_module_once_have_no_problem(rosters):
  assert run_tests.roster_problems() == []


def test_a_roster_entry_naming_no_file_is_a_problem(rosters):
  (rosters / 'thing/api_test.py').unlink()

  assert run_tests.roster_problems() == ['thing/api_test.py is in a roster but is no file']


def test_a_test_module_no_roster_names_is_a_problem(rosters):
  (rosters / 'thing/new_test.py').write_text('')

  assert run_tests.roster_problems() == ['thing/new_test.py is in no roster']


def test_a_test_module_two_rosters_name_is_a_problem(rosters, monkeypatch):
  monkeypatch.setattr(run_tests, 'PYTEST_FILES', [*run_tests.PYTEST_FILES, 'thing/e2e_test.py'])

  assert run_tests.roster_problems() == ['thing/e2e_test.py is in 2 rosters']


def test_the_gate_refuses_to_start_over_a_roster_problem(rosters, monkeypatch):
  ran = []
  monkeypatch.setattr(run_tests, 'STAGES', [run_tests.Stage('types', lambda: ran.append(1), '')])
  (rosters / 'thing/api_test.py').unlink()

  with pytest.raises(SystemExit, match='thing/api_test.py is in a roster but is no file'):
    run_tests.main([])
  assert ran == []


@pytest.fixture
def noted(rosters, monkeypatch):
  """the checkout above committed clean, with the gate noting what passes on its tree."""
  subprocess.run(('git', 'add', '.'), cwd=rosters, check=True, capture_output=True)
  subprocess.run(('git', 'commit', '-m', 'rosters'), cwd=rosters, check=True, capture_output=True)
  monkeypatch.setattr(green_trees, 'clean_tree', CLEAN_TREE)
  return rosters


def probe_stages(monkeypatch, ran, *, unit_work=run_tests.WHOLE, unit_fails=False):
  def unit() -> None:
    ran.append('unit')
    if unit_fails:
      run_tests.step('probe')
      raise subprocess.CalledProcessError(1, ('probe',), output='')

  monkeypatch.setattr(
    run_tests,
    'STAGES',
    [
      run_tests.Stage('types', lambda: ran.append('types'), 'a probe'),
      run_tests.Stage('unit', unit, 'a probe', work=unit_work),
    ],
  )


def test_a_stage_that_passed_on_the_tree_runs_again_only_on_rerun(noted, monkeypatch, capsys):
  ran = []
  probe_stages(monkeypatch, ran)

  assert run_tests.main([]) is None
  assert run_tests.main([]) is None
  assert ran == ['types', 'unit']
  assert capsys.readouterr().err.endswith('gate: types ok | unit ok\n')

  assert run_tests.main(['run-tests', '--rerun']) is None
  assert ran == ['types', 'unit', 'types', 'unit']


def test_a_failed_stage_runs_again_on_the_same_tree(noted, monkeypatch):
  ran = []
  probe_stages(monkeypatch, ran, unit_fails=True)

  assert run_tests.main([]) == 1
  assert run_tests.main([]) == 1
  assert ran == ['types', 'unit', 'unit']


def test_a_whole_pass_covers_a_narrowed_stage_but_not_the_reverse(noted, monkeypatch):
  ran = []
  probe_stages(monkeypatch, ran, unit_work=run_tests.work_digest(['thing/api_test.py']))
  assert run_tests.main([]) is None
  probe_stages(monkeypatch, ran)
  assert run_tests.main([]) is None
  probe_stages(monkeypatch, ran, unit_work=run_tests.work_digest(['thing/store_test.py']))
  assert run_tests.main([]) is None

  assert ran == ['types', 'unit', 'unit']


def test_a_worktree_with_edits_notes_nothing(noted, monkeypatch):
  ran = []
  probe_stages(monkeypatch, ran)
  (noted / 'thing/store.py').write_text('CHANGED = 1\n')

  assert run_tests.main([]) is None
  assert run_tests.main([]) is None
  assert ran == ['types', 'unit', 'types', 'unit']
