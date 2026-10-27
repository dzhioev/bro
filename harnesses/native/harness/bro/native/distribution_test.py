import configparser
import email.parser
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[5]
_PROJECTS = {
  'bro': _ROOT,
  'bro-bench': _ROOT / 'bench',
  'bro-benchmark': _ROOT / 'benchmark',
  'bro-dev': _ROOT / 'dev',
  'bro-filetypes': _ROOT / 'filetypes',
  'bro-claude': _ROOT / 'harnesses' / 'claude' / 'harness',
  'bro-claude-trails': _ROOT / 'harnesses' / 'claude' / 'trails',
  'bro-native': _ROOT / 'harnesses' / 'native' / 'harness',
  'bro-native-trails': _ROOT / 'harnesses' / 'native' / 'trails',
  'bro-oops': _ROOT / 'oops',
  'bro-oops-cdk': _ROOT / 'oops' / 'cdk',
  'bro-ride': _ROOT / 'ride',
  'bro-wire-responses': _ROOT / 'wires' / 'responses',
  'bro-wire-messages': _ROOT / 'wires' / 'messages',
  'bro-wire-chat': _ROOT / 'wires' / 'chat',
  'bro-openai': _ROOT / 'llms' / 'openai',
  'bro-anthropic': _ROOT / 'llms' / 'anthropic',
}
_NATIVE_DOMAIN = {
  'bro/fork.py',
  'bro/inbox.py',
  'bro/jobs.py',
  'bro/launch/call.py',
  'bro/launch/call_tui.py',
  'bro/launch/resume.py',
  'bro/launch/run.py',
  'bro/native/dev_mcp.py',
  'bro/native/harness.py',
  'bro/native/llm.py',
  'bro/native/echo.py',
  'bro/native/echo_declaration.py',
  'bro/native/loop.py',
  'bro/native/runner.py',
  'bro/run.py',
  'bro/trails/record/bro.py',
}
_NATIVE_SUPPORT = {
  'bro/native/__init__.py',
  'bro/native/_entrypoints.py',
}
_CORE_DOMAIN = {
  'bro/bro.py',
  'bro/launch/broxy.py',
  'bro/launch/hold.py',
  'bro/launch/llm_flags.py',
  'bro/trails/record/spine.py',
}
_NAMESPACE_MODULES = {
  'bro.extra.github.api': 'bro/extra/github/api.py',
  'bro.extra.github.poll_pr': 'bro/extra/github/poll_pr.py',
  'bro.pdf': 'bro/pdf/__init__.py',
  'bro.launch.call': 'bro/launch/call.py',
  'bro.launch.hold': 'bro/launch/hold.py',
  'bro.oops.cdk.app': 'bro/oops/cdk/app.py',
  'bro.oops.config': 'bro/oops/config.py',
  'bro.claude.harness': 'bro/claude/harness.py',
  'bro.claude_trails.format': 'bro/claude_trails/format.py',
  'bro.native_trails.format': 'bro/native_trails/format.py',
  'bro.trails.record.bro': 'bro/trails/record/bro.py',
  'bro.trails.record.spine': 'bro/trails/record/spine.py',
}


def _metadata(wheel: Path):
  with zipfile.ZipFile(wheel) as archive:
    path = next(name for name in archive.namelist() if name.endswith('.dist-info/METADATA'))
    return email.parser.Parser().parsestr(archive.read(path).decode())


@pytest.fixture(scope='module')
def wheels(tmp_path_factory) -> dict[str, Path]:
  output_directory = tmp_path_factory.mktemp('distribution-wheels')
  result = {}
  for expected_name, project in _PROJECTS.items():
    subprocess.run(
      ['uv', 'build', '--wheel', str(project), '--out-dir', str(output_directory)],
      capture_output=True,
      check=True,
    )
    matches = [
      wheel for wheel in output_directory.glob('*.whl') if _metadata(wheel)['Name'] == expected_name
    ]
    assert len(matches) == 1
    result[expected_name] = matches[0]
  return result


@pytest.fixture(scope='module')
def wheel_files(wheels) -> dict[str, set[str]]:
  result = {}
  for distribution, wheel in wheels.items():
    with zipfile.ZipFile(wheel) as archive:
      result[distribution] = {
        name for name in archive.namelist() if not name.endswith('/') and '.dist-info/' not in name
      }
  return result


def _entry_points(wheel: Path) -> configparser.ConfigParser:
  metadata = configparser.ConfigParser()
  with zipfile.ZipFile(wheel) as archive:
    path = next(name for name in archive.namelist() if name.endswith('.dist-info/entry_points.txt'))
    metadata.read_string(archive.read(path).decode())
  return metadata


def _requirement_name(requirement: str) -> str:
  return requirement.partition(';')[0].partition('[')[0].partition('=')[0].strip().lower()


def _project_dependencies(path: Path) -> set[str]:
  project = tomllib.loads(path.read_text())['project']
  return {_requirement_name(requirement) for requirement in project.get('dependencies', [])}


def _assert_namespace_modules(python_path: str) -> None:
  checks = ';'.join(
    f'assert importlib.util.find_spec({module!r}).origin.endswith({path!r})'
    for module, path in _NAMESPACE_MODULES.items()
  )
  environment = {**os.environ, 'PYTHONPATH': python_path}
  subprocess.run(
    [sys.executable, '-S', '-c', f'import importlib.util;{checks}'],
    cwd='/tmp',
    env=environment,
    capture_output=True,
    text=True,
    check=True,
  )


def test_native_domain_ships_only_in_bro_native(wheel_files):
  native_modules = {path for path in wheel_files['bro-native'] if path.endswith('.py')}
  assert native_modules == _NATIVE_DOMAIN | _NATIVE_SUPPORT
  assert _NATIVE_DOMAIN.isdisjoint(wheel_files['bro'])
  assert _CORE_DOMAIN <= wheel_files['bro']
  assert _CORE_DOMAIN.isdisjoint(wheel_files['bro-native'])


def test_distributions_do_not_ship_the_same_source_path(wheel_files):
  owners: dict[str, str] = {}
  for distribution, files in wheel_files.items():
    for path in files:
      assert path not in owners, f'{path} ships in both {owners.get(path)} and {distribution}'
      owners[path] = distribution


def test_native_distribution_owns_its_console_scripts(wheels):
  native_scripts = _entry_points(wheels['bro-native'])['console_scripts']
  core_scripts = _entry_points(wheels['bro'])['console_scripts']
  assert native_scripts['bro'] == 'bro.native._entrypoints:bro_run'
  assert native_scripts['bro.run'] == 'bro.native._entrypoints:bro_run'
  assert native_scripts['bro.native.llm'] == 'bro.native._entrypoints:bro_native_llm'
  assert {'bro', 'bro.run', 'bro.native.llm'}.isdisjoint(core_scripts)


def test_harnesses_register_from_their_own_distributions(wheels):
  assert _entry_points(wheels['bro-native'])['bro.harnesses']['bro'] == ('bro.native.harness:BRO')
  assert _entry_points(wheels['bro-claude'])['bro.harnesses']['claude'] == (
    'bro.claude.harness:CLAUDE'
  )


def test_trail_formats_register_from_their_own_distributions(wheels):
  assert not _entry_points(wheels['bro']).has_section('bro.trail_formats')
  assert dict(_entry_points(wheels['bro-claude-trails'])['bro.trail_formats']) == {
    'claude': 'bro.claude_trails.format:CLAUDE_FORMAT',
  }

  assert dict(_entry_points(wheels['bro-native-trails'])['bro.trail_formats']) == {
    'bro': 'bro.native_trails.format:BRO_FORMAT',
  }


def test_native_format_ships_only_its_own_package(wheel_files):
  assert wheel_files['bro-native-trails'] == {
    'bro/native_trails/__init__.py',
    'bro/native_trails/format.py',
  }
  assert not any(path.startswith('bro/native_trails/') for path in wheel_files['bro'])


def test_core_registers_file_toolset(wheels):
  assert _entry_points(wheels['bro'])['bro.toolsets']['file'] == 'bro.files.mcp:toolset'


def test_dependency_edges_follow_the_distribution_boundaries(wheels):
  native_requirements = {
    _requirement_name(requirement)
    for requirement in (_metadata(wheels['bro-native']).get_all('Requires-Dist') or [])
  }
  assert native_requirements == {
    'aiohttp',
    'bro',
    'bro-ride',
    'bro-native-trails',
    'humanize',
    'mcp',
    'rich',
    'textual',
    'trafilatura',
    'tree-sitter',
    'tree-sitter-bash',
  }
  assert {
    _requirement_name(requirement)
    for requirement in (_metadata(wheels['bro-native-trails']).get_all('Requires-Dist') or [])
  } == {'bro'}
  assert _project_dependencies(_ROOT / 'ride' / 'pyproject.toml') == {
    'bro',
    'certifi',
    'humanize',
    'mcp',
    'rich',
    'textual',
    'tree-sitter',
    'tree-sitter-bash',
  }
  assert _project_dependencies(_ROOT / 'dev' / 'pyproject.toml') == {
    'bro',
    'bro-openai',
    'markdown-it-py',
    'pytest',
  }
  assert _project_dependencies(_ROOT / 'oops' / 'pyproject.toml') == {
    'bro',
    'bro-dev',
    'bro-openai',
  }
  assert _project_dependencies(_ROOT / 'oops' / 'cdk' / 'pyproject.toml') >= {
    'aws-cdk-lib',
    'bro-oops',
  }
  assert _project_dependencies(_ROOT / 'filetypes' / 'pyproject.toml') == {
    'bro',
    'pydantic',
    'pypdfium2',
  }
  assert _project_dependencies(_ROOT / 'bench' / 'pyproject.toml') == {'bro'}
  assert _project_dependencies(_ROOT / 'benchmark' / 'pyproject.toml') >= {
    'bro',
    'bro-bench',
    'bro-ride',
    'bro-native-trails',
    'bro-claude-trails',
  }
  root = tomllib.loads((_ROOT / 'pyproject.toml').read_text())
  assert {'agent', 'ride'}.isdisjoint(root['project'].get('optional-dependencies', {}))


def test_shared_namespaces_resolve_from_editable_source_roots():
  _assert_namespace_modules(
    os.pathsep.join(
      str(_ROOT / directory)
      for directory in (
        '.',
        'dev',
        'harnesses/native/harness',
        'harnesses/native/trails',
        'harnesses/claude/harness',
        'harnesses/claude/trails',
        'oops',
        'oops/cdk',
        'filetypes',
      )
    )
  )


def test_bundled_ride_checks_native_without_the_runtime_on_path(wheels, tmp_path):
  venv = tmp_path / 'native-runtime'
  subprocess.run(
    ['uv', 'venv', '--python', sys.executable, str(venv)], check=True, capture_output=True
  )
  subprocess.run(
    [
      'uv',
      'pip',
      'install',
      '--python',
      str(venv / 'bin' / 'python'),
      str(wheels['bro']),
      str(wheels['bro-ride']),
      str(wheels['bro-wire-messages']),
      str(wheels['bro-native']),
      str(wheels['bro-native-trails']),
      str(wheels['bro-wire-responses']),
    ],
    check=True,
    capture_output=True,
  )

  subprocess.run(
    [str(venv / 'bin' / 'ride'), 'check-harness', 'bro'],
    check=True,
    capture_output=True,
    env={**os.environ, 'PATH': '/usr/bin:/bin'},
  )


def test_shared_namespaces_resolve_after_wheel_install(wheels, tmp_path):
  for wheel in wheels.values():
    with zipfile.ZipFile(wheel) as archive:
      archive.extractall(tmp_path)
  _assert_namespace_modules(str(tmp_path))


def test_pdf_type_registers_from_its_own_wheel(wheels, wheel_files):
  assert _entry_points(wheels['bro-filetypes'])['bro.file_types']['pdf'] == 'bro.pdf:TYPE'
  assert {
    'bro/pdf/__init__.py',
    'bro/pdf/engine.py',
    'bro/pdf/provider.py',
    'bro/pdf/manual.md',
  } <= wheel_files['bro-filetypes']
  assert not any(path.startswith('bro/pdf/') for path in wheel_files['bro'])
  requirements = _metadata(wheels['bro-filetypes']).get_all('Requires-Dist')
  assert requirements is not None
  assert 'pypdfium2==5.14.0' in requirements


@pytest.mark.parametrize('name', ['chat', 'messages', 'responses'])
def test_wire_wheels_separate_optional_clients_from_sdk_free_decoders(wheels, wheel_files, name):
  distribution = f'bro-wire-{name}'
  assert dict(_entry_points(wheels[distribution])['bro.llm.wires']) == {
    name: f'bro.wire_{name}.decode'
  }
  expected = {f'bro/wire_{name}/__init__.py', f'bro/wire_{name}/decode.py'}
  entries = _entry_points(wheels[distribution])
  if entries.has_section('bro.llm.wire_clients'):
    expected.add(f'bro/wire_{name}/client.py')
    if name == 'responses':
      expected.add(f'bro/wire_{name}/content.py')
    assert set(entries['bro.llm.wire_clients']) == {name}
    assert _metadata(wheels[distribution]).get_all('Provides-Extra') == ['client']
  assert wheel_files[distribution] == expected
  requirements = _metadata(wheels[distribution]).get_all('Requires-Dist')
  assert requirements is not None
  assert {
    _requirement_name(requirement) for requirement in requirements if 'extra ==' not in requirement
  } == {'bro'}


def test_wire_decode_wheels_work_without_sdks_or_harnesses(wheels, tmp_path):
  for distribution in ('bro', 'bro-wire-chat', 'bro-wire-messages', 'bro-wire-responses'):
    with zipfile.ZipFile(wheels[distribution]) as archive:
      archive.extractall(tmp_path)
  probe = """
import sys
from bro.llm import wires
from bro.llm.usage import from_wire_counts
for name, response in (
  ('responses', {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'reply'}]}]}),
  ('messages', {'content': [{'type': 'text', 'text': 'reply'}]}),
  ('chat', {'choices': [{'finish_reason': 'stop', 'message': {'content': 'reply'}}]}),
):
  assert wires.decode(name, response).reply == 'reply'
assert from_wire_counts('responses', {'input_tokens': 5, 'output_tokens': 2})['input'] == 5
assert wires.registry.legacy_name() == 'responses'
assert not any(name in sys.modules for name in ('openai', 'anthropic', 'bro.native', 'ride'))
"""
  subprocess.run(
    [sys.executable, '-S', '-c', probe],
    cwd='/tmp',
    env={**os.environ, 'PYTHONPATH': str(tmp_path)},
    capture_output=True,
    text=True,
    check=True,
  )


def test_native_trail_format_wheel_projects_without_a_harness_or_sdk(wheels, tmp_path):
  for distribution in ('bro', 'bro-native-trails', 'bro-wire-responses'):
    with zipfile.ZipFile(wheels[distribution]) as archive:
      archive.extractall(tmp_path)
  probe = """
import importlib.metadata
import sys
from bro.trails.backends import get_format
entry = next(entry for entry in importlib.metadata.entry_points(group='bro.trail_formats') if entry.name == 'bro')
assert entry.dist.name == 'bro-native-trails'
events = get_format('bro').project({
  'step_id': 3,
  'kind': 'llm_call',
  'body': {'response': {'model': 'model', 'status': 'completed', 'output': [
    {'type': 'message', 'content': [{'type': 'output_text', 'text': 'recorded reply'}]},
  ]}},
})
assert [event['type'] for event in events] == ['llm_call', 'assistant']
assert events[1]['content'] == 'recorded reply'
assert events[1]['terminal'] is True
assert not any(name in sys.modules for name in ('openai', 'anthropic', 'bro.native', 'ride'))
"""
  subprocess.run(
    [sys.executable, '-S', '-c', probe],
    cwd='/tmp',
    env={**os.environ, 'PYTHONPATH': str(tmp_path)},
    capture_output=True,
    text=True,
    check=True,
  )


def test_claude_harness_wheel_contains_only_its_package_and_assets(wheel_files):
  files = wheel_files['bro-claude']
  assert all(path.startswith('bro/claude/') for path in files)
  assert {
    'bro/claude/harness.py',
    'bro/claude/credentials.py',
    'bro/claude/image/claude-code-version',
    'bro/claude/image/Dockerfile',
    'bro/claude/plugin/.claude-plugin/plugin.json',
  } <= files
  assert 'bro/claude/image/bump-claude-code.sh' not in files
  assert not any(path.endswith('.test.ts') for path in files)


def test_claude_format_wheel_contains_only_its_own_package(wheel_files):
  assert wheel_files['bro-claude-trails'] == {
    'bro/claude_trails/__init__.py',
    'bro/claude_trails/format.py',
    'bro/claude_trails/lineage.py',
  }


def test_claude_distribution_owns_its_console_scripts_and_registrations(wheels):
  entries = _entry_points(wheels['bro-claude'])
  assert dict(entries['console_scripts']) == {
    f'bro.claude.{name.replace("_", "-")}': f'bro.claude._entrypoints:bro_claude_{name}'
    for name in (
      'command_gate',
      'read_gate',
      'session_end',
      'statusline',
      'trail_recorder',
      'watch_waiter',
    )
  }
  assert dict(entries['bro.mcp.targets']) == {
    'persona': 'bro.claude.assembly:resolve_persona_target',
  }
  assert dict(entries['bro.credentials']) == {'claude_code': 'bro.claude.credentials:KIND'}
  ride = _entry_points(wheels['bro-ride'])
  assert not any(name.startswith('ride.claude.') for name in ride['console_scripts'])
  assert not ride.has_section('bro.harnesses')
  assert not ride.has_section('bro.mcp.targets')


def test_claude_dependency_edges_follow_the_distribution_boundaries(wheels):
  assert {
    _requirement_name(requirement)
    for requirement in (_metadata(wheels['bro-claude-trails']).get_all('Requires-Dist') or [])
  } == {'bro', 'bro-wire-messages'}
  assert {
    _requirement_name(requirement)
    for requirement in (_metadata(wheels['bro-claude']).get_all('Requires-Dist') or [])
  } == {'bro', 'bro-ride', 'bro-claude-trails', 'bro-wire-messages', 'pyyaml', 'watchfiles'}


def test_claude_trail_format_wheel_projects_without_a_harness_or_sdk(wheels, tmp_path):
  for distribution in ('bro', 'bro-claude-trails', 'bro-wire-messages'):
    with zipfile.ZipFile(wheels[distribution]) as archive:
      archive.extractall(tmp_path)
  probe = """
import importlib.metadata
import json
import sys
from bro.trails.backends import get_format
entry = next(entry for entry in importlib.metadata.entry_points(group='bro.trail_formats') if entry.name == 'claude')
assert entry.dist.name == 'bro-claude-trails'
record = {'type': 'assistant', 'message': {'model': 'model', 'content': [
  {'type': 'text', 'text': 'recorded reply'},
], 'usage': {'input_tokens': 5, 'output_tokens': 2}}}
events = get_format('claude').project({
  'step_id': 3, 'kind': 'assistant', 'body': json.dumps(record), 'usage': record['message']['usage'],
})
assert [event['type'] for event in events] == ['llm_call', 'assistant']
assert events[1]['content'] == 'recorded reply'
assert events[1]['terminal'] is True
assert not any(name in sys.modules for name in ('openai', 'anthropic', 'bro.native', 'bro.claude', 'ride'))
"""
  subprocess.run(
    [sys.executable, '-S', '-c', probe],
    cwd='/tmp',
    env={**os.environ, 'PYTHONPATH': str(tmp_path)},
    capture_output=True,
    text=True,
    check=True,
  )
