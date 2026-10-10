import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ride import provisioning
from ride.harness import RuntimeImage
from ride.workspace import build_context, docker


@pytest.mark.parametrize('name', ['bro', 'claude'])
def test_single_harness_installation_resolves_builds_and_bootstraps(name, tmp_path):
  script = """
import importlib.abc
import importlib.metadata
import sys
from pathlib import Path

name, root = sys.argv[1], Path(sys.argv[2])
blocked = 'ride.claude' if name == 'bro' else 'bro.native'
class RejectOtherHarness(importlib.abc.MetaPathFinder):
  def find_spec(self, fullname, path=None, target=None):
    if fullname == blocked or fullname.startswith(blocked + '.'):
      raise AssertionError('uninstalled harness imported: ' + fullname)
sys.meta_path.insert(0, RejectOtherHarness())
import bro.harness as registry
module = 'bro.native.harness:BRO' if name == 'bro' else 'ride.claude.harness:CLAUDE'
registry._entry_points = lambda: (importlib.metadata.EntryPoint(name=name, value=module, group='bro.harnesses'),)
from ride.harness import get_harness
from ride import provisioning
from ride.workspace import build_context, docker
harness = get_harness(name)
recipe = harness.resolve_llm('echo:' if name == 'bro' else None, 'bro')
assert recipe.TYPE == ('echo' if name == 'bro' else 'claude-code')
checks = []
if name == 'claude':
  harness.check_runtime = lambda: checks.append('checked')
provisioning.setup_harnesses()
assert checks == ([] if name == 'bro' else ['checked'])
entries, arguments = build_context.runtime_inputs()
text = entries[build_context.DOCKERFILE_PATH][0].decode()
assert ('claude plugin install' in text) == (name == 'claude')
assert ('CLAUDE_CODE_VERSION' in arguments) == (name == 'claude')
calls = []
docker.subprocess.run = lambda argv, **kwargs: calls.append((argv, kwargs))
docker.build_runtime_image('single:test', '3.12')
assert calls[0][1]['input'] == build_context.assemble_runtime(entries)
assert bool(docker.runtime_image_tag('3.12'))
if name == 'bro':
  assert provisioning.provision_bundle(root, ('linux', 'x86_64', 'glibc')) == {'bro': {}}
assert not any(module == blocked or module.startswith(blocked + '.') for module in sys.modules)
"""
  subprocess.run([sys.executable, '-c', script, name, str(tmp_path)], check=True)


def test_arbitrary_harness_contributions_reach_the_image_and_identity(monkeypatch, tmp_path):
  image = RuntimeImage(
    dockerfile='COPY .bro-container/harnesses/extra/runtime /opt/extra\n',
    files={'runtime': b'release-one'},
    build_arguments={'EXTRA_RELEASE': 'one'},
  )
  harness = SimpleNamespace(runtime_image=lambda: image)
  monkeypatch.setattr(build_context, 'installed_harness_names', lambda: ('extra',))
  monkeypatch.setattr(build_context, 'get_harness', lambda name: harness)
  first = docker.runtime_image_tag('3.12')
  entries, arguments = build_context.runtime_inputs()
  assert entries['.bro-container/harnesses/extra/runtime'][0] == b'release-one'
  assert image.dockerfile.encode() in entries[build_context.DOCKERFILE_PATH][0]
  calls = []
  monkeypatch.setattr(docker.subprocess, 'run', lambda argv, **kwargs: calls.append((argv, kwargs)))
  docker.build_runtime_image('extra:test', '3.12')
  assert 'EXTRA_RELEASE=one' in calls[0][0]
  assert calls[0][1]['input'] == build_context.assemble_runtime(entries)
  image.files['runtime'] = b'release-two'
  assert docker.runtime_image_tag('3.12') != first
  second = docker.runtime_image_tag('3.12')
  image.build_arguments['EXTRA_RELEASE'] = 'two'
  assert docker.runtime_image_tag('3.12') != second


def test_bootstrap_and_bundle_provisioning_visit_only_installed_harnesses(
  monkeypatch, tmp_path, capsys
):
  setups = []
  asset = tmp_path / 'extra' / 'runtime'

  def install(root, target):
    assert root == tmp_path
    assert target == ('test',)
    asset.parent.mkdir()
    asset.write_bytes(b'engine')
    return ('extra/runtime',)

  harness = SimpleNamespace(setup_runtime=lambda: setups.append('extra'), provision_bundle=install)
  monkeypatch.setattr(provisioning, 'installed_harness_names', lambda: ('extra',))
  monkeypatch.setattr(provisioning, 'get_harness', lambda name: harness)

  assert provisioning.main(['provision', '--bundle', str(tmp_path), '--target', 'test']) is None
  provisioning.setup_harnesses()
  assert setups == ['extra']
  assert json.loads(capsys.readouterr().out) == {
    'extra': {'extra/runtime': hashlib.sha256(b'engine').hexdigest()}
  }


@pytest.mark.parametrize(
  'path', ['', '.', '../escape', '/absolute', 'venv/override', 'bin/ride', 'a/../b']
)
def test_refuses_invalid_asset_paths(path):
  with pytest.raises(ValueError, match='harness asset'):
    provisioning.bundle_file(path)


def test_bundle_provisioning_fails_on_missing_declared_files(monkeypatch, tmp_path):
  monkeypatch.setattr(provisioning, 'installed_harness_names', lambda: ('extra',))
  monkeypatch.setattr(
    provisioning,
    'get_harness',
    lambda name: SimpleNamespace(provision_bundle=lambda root, target: ('absent',)),
  )
  with pytest.raises(FileNotFoundError):
    provisioning.provision_bundle(tmp_path, ('test',))


def test_harnesses_cannot_override_shared_image_arguments(monkeypatch):
  monkeypatch.setattr(build_context, 'installed_harness_names', lambda: ('extra',))
  monkeypatch.setattr(
    build_context,
    'get_harness',
    lambda name: SimpleNamespace(
      runtime_image=lambda: RuntimeImage(build_arguments={'PYTHON_VERSION': 'wrong'})
    ),
  )
  with pytest.raises(ValueError, match='claimed twice'):
    build_context.runtime_inputs()


def test_native_help_does_not_describe_another_harness(capsys):
  from ride.cli import main

  with pytest.raises(SystemExit) as exit:
    main(['ride', 'solo', '--harness', 'bro', '--help'])
  assert exit.value.code == 0
  help_text = capsys.readouterr().out
  assert 'claude --effort' not in help_text
  assert "Claude's own fast-mode" not in help_text
  assert '--effort' in help_text and '--fast' in help_text


def test_shared_launch_flags_import_no_harness_implementation():
  script = """
import importlib.abc
import sys
class RejectHarness(importlib.abc.MetaPathFinder):
  def find_spec(self, fullname, path=None, target=None):
    if fullname.startswith(('ride.claude', 'bro.native', 'bro.llm.llms.claude_code')):
      raise AssertionError('harness implementation imported: ' + fullname)
sys.meta_path.insert(0, RejectHarness())
from bro.base.args import Parser
from bro.launch.llm_flags import add_llm_flags, selection_from_args
parser = Parser(add_help=False)
add_llm_flags(parser, effort_help='effort', fast_help='fast')
selection = selection_from_args(parser.parse(['test', '--llm', 'echo:']))
assert selection.provider == 'echo'
"""
  subprocess.run([sys.executable, '-c', script], check=True)


@pytest.mark.parametrize('parent', [False, True])
@pytest.mark.parametrize('absolute', [False, True])
def test_bundle_provisioning_refuses_symlinks_in_asset_paths(
  monkeypatch, tmp_path, parent, absolute
):
  payload = tmp_path / 'payload'
  payload.mkdir()
  (payload / 'binary').write_bytes(b'engine')
  alias = tmp_path / 'alias'
  target = payload if parent else payload / 'binary'
  alias.symlink_to(target if absolute else target.relative_to(tmp_path))
  relative = 'alias/binary' if parent else 'alias'
  monkeypatch.setattr(provisioning, 'installed_harness_names', lambda: ('extra',))
  monkeypatch.setattr(
    provisioning,
    'get_harness',
    lambda name: SimpleNamespace(provision_bundle=lambda root, target: (relative,)),
  )

  with pytest.raises(ValueError, match='symbolic links'):
    provisioning.provision_bundle(tmp_path, ('test',))
