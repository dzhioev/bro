import shutil
import sys
from pathlib import Path

import pytest

from bro.workspace.paths import ISOLATION_ENV
from ride import host_check
from ride.host_check import (
  Fix,
  Platform,
  Prerequisite,
  Probe,
  UnsupportedHost,
  check_host,
  check_this_host,
  host_platform,
  prerequisites,
)
from ride.workspace.build_context import UV_VERSION_FILE

PINNED_UV = UV_VERSION_FILE.read_text().strip()

_DOCKER = """#!/bin/bash
case "$1" in
  --version) echo 'Docker version 28.1.1, build 4eba377' ;;
  info)
    if [ -n "$DAEMON_DOWN" ]; then
      echo 'Cannot connect to the Docker daemon at unix:///var/run/docker.sock.' >&2
      exit 1
    fi
    echo 28.1.1
    ;;
  buildx) echo 'github.com/docker/buildx v0.20.0' ;;
esac
"""


def _command(directory: Path, name: str, body: str) -> None:
  path = directory / name
  path.write_text(body)
  path.chmod(0o755)


@pytest.fixture
def host(tmp_path, monkeypatch):
  """a PATH holding exactly the stub tools a test writes into it, under a home whose
  default bin directory is uv's install directory."""
  binaries = tmp_path / 'bin'
  binaries.mkdir()
  monkeypatch.setenv('PATH', str(binaries))
  monkeypatch.setenv('HOME', str(tmp_path / 'home'))
  monkeypatch.delenv('UV_INSTALL_DIR', raising=False)
  monkeypatch.delenv('XDG_BIN_HOME', raising=False)
  return binaries


def _uv_directory(tmp_path: Path) -> Path:
  return tmp_path / 'home' / '.local' / 'bin'


def _uv(version: str) -> str:
  return f'#!/bin/bash\necho "uv {version} (stub)"\n'


def _complete(binaries: Path, *, uv: str = PINNED_UV) -> None:
  _command(binaries, 'git', '#!/bin/bash\necho git version 2.47.3\n')
  _command(binaries, 'uv', _uv(uv))
  _command(binaries, 'docker', _DOCKER)


def test_a_complete_host_reports_every_prerequisite_ok(host, capsys):
  _complete(host)

  assert check_host(prerequisites(), Platform.UBUNTU, install=False) == 0

  lines = capsys.readouterr().out.splitlines()
  assert [line.partition(':')[0] for line in lines] == [item.name for item in prerequisites()]
  assert all(': ok (' in line for line in lines)


def test_a_uv_off_the_pin_is_installed_into_the_uv_directory(host, tmp_path, capsys):
  _complete(host, uv='0.0.1')

  assert check_host(prerequisites(), Platform.MACOS, install=False) == 1

  out = capsys.readouterr().out
  assert f'uv 0.0.1 at {host / "uv"} is installed, {PINNED_UV} is pinned' in out
  fix = next(line for line in out.splitlines() if line.startswith('  fix: curl'))
  # uv's own installer, at the pinned release
  assert f'https://astral.sh/uv/{PINNED_UV}/install.sh' in fix
  assert f'UV_INSTALL_DIR={_uv_directory(tmp_path)}' in fix
  assert out.splitlines()[-1] == '`ride check-host --install` runs the fixes for uv'


def test_an_install_another_uv_shadows_reports_the_path_instead_of_reinstalling(
  host, tmp_path, capsys
):
  _complete(host, uv='0.0.1')
  for name in ('bash', 'cat', 'cp', 'env', 'mkdir', 'sh'):
    target = shutil.which(name, path='/usr/bin:/bin')
    assert target is not None
    (host / name).symlink_to(target)
  # what uv's installer does: put the pinned uv into the directory it is given
  _command(tmp_path, 'pinned-uv', _uv(PINNED_UV))
  (tmp_path / 'installer.sh').write_text(
    f'mkdir -p "$UV_INSTALL_DIR" && cp {tmp_path / "pinned-uv"} "$UV_INSTALL_DIR/uv"\n'
  )
  _command(host, 'curl', f'#!/bin/bash\ncat {tmp_path / "installer.sh"}\n')

  assert check_host(prerequisites(), Platform.MACOS, install=True) == 1

  out = capsys.readouterr().out
  assert out.count('installing uv') == 1
  installed = _uv_directory(tmp_path) / 'uv'
  assert f'PATH does not reach the pinned uv at {installed} first' in out
  assert f'  fix: export PATH={_uv_directory(tmp_path)}:"$PATH"' in out
  assert '  fix: curl' not in out


def _mkdir(directory: Path) -> Path:
  directory.mkdir(parents=True)
  return directory


def test_a_pinned_uv_off_path_is_reported_with_the_path(host, tmp_path, capsys):
  _complete(host)
  (host / 'uv').unlink()
  _command(_mkdir(_uv_directory(tmp_path)), 'uv', _uv(PINNED_UV))

  assert check_host(prerequisites(), Platform.UBUNTU, install=False) == 1

  out = capsys.readouterr().out
  assert 'uv is not on PATH, and PATH does not reach the pinned uv' in out
  assert 'runs the fixes for' not in out


@pytest.mark.parametrize(
  ('variables', 'directory'),
  [
    ({'UV_INSTALL_DIR': 'uv-dir', 'XDG_BIN_HOME': 'xdg-bin'}, 'uv-dir'),
    ({'XDG_BIN_HOME': 'xdg-bin'}, 'xdg-bin'),
    ({}, 'home/.local/bin'),
  ],
)
def test_the_uv_directory_honors_the_installers_variables(
  host, tmp_path, monkeypatch, variables, directory
):
  for name, value in variables.items():
    monkeypatch.setenv(name, str(tmp_path / value))

  uv = next(item for item in prerequisites() if item.name == 'uv')

  assert f'UV_INSTALL_DIR={tmp_path / directory} ' in uv.fixes[Platform.UBUNTU].command


def test_a_missing_docker_leaves_what_builds_on_it_unmet(host, capsys):
  _complete(host)
  (host / 'docker').unlink()

  assert check_host(prerequisites(), Platform.MACOS, install=False) == 1

  lines = capsys.readouterr().out.splitlines()
  assert 'docker (runs boxed sessions): docker is not on PATH' in lines
  assert 'docker daemon: needs docker' in lines
  assert 'docker buildx: needs docker' in lines
  assert lines[-1] == '`ride check-host --install` runs the fixes for docker'


def test_a_daemon_that_does_not_answer_is_reported_with_dockers_own_reason(
  host, capsys, monkeypatch
):
  _complete(host)
  monkeypatch.setenv('DAEMON_DOWN', '1')

  assert check_host(prerequisites(), Platform.UBUNTU, install=False) == 1

  out = capsys.readouterr().out
  assert 'Cannot connect to the Docker daemon' in out
  assert 'runs the fixes for' not in out


def _marked(directory: Path, name: str, *, automatic: bool, requires=()) -> Prerequisite:
  """holds once its marker file exists; its fix creates the marker."""
  marker = directory / name

  def probe() -> Probe:
    return Probe('marked') if marker.exists() else Probe('', problem=f'no {name} marker')

  return Prerequisite(
    name=name,
    purpose='a test prerequisite',
    probe=probe,
    fixes=dict.fromkeys(Platform, Fix(f'touch {marker}', automatic=automatic)),
    requires=requires,
  )


def test_install_runs_only_the_automatic_fixes_of_failing_prerequisites(tmp_path, capsys):
  (tmp_path / 'held').touch()
  prerequisites = (
    _marked(tmp_path, 'held', automatic=True),
    _marked(tmp_path, 'automatic', automatic=True),
    _marked(tmp_path, 'manual', automatic=False),
  )

  assert check_host(prerequisites, Platform.UBUNTU, install=True) == 1

  assert (tmp_path / 'automatic').exists()
  assert not (tmp_path / 'manual').exists()
  out = capsys.readouterr().out
  assert out.count('installing ') == 1
  assert 'automatic: ok (marked)' in out
  assert f'  fix: touch {tmp_path / "manual"}' in out


def test_install_skips_a_fix_whose_requirement_stays_unmet(tmp_path):
  prerequisites = (
    _marked(tmp_path, 'base', automatic=False),
    _marked(tmp_path, 'dependent', automatic=True, requires=('base',)),
  )

  assert check_host(prerequisites, Platform.UBUNTU, install=True) == 1

  assert not (tmp_path / 'dependent').exists()


def test_a_failing_install_stops_before_later_fixes(tmp_path):
  failing = Prerequisite(
    name='failing',
    purpose='a test prerequisite',
    probe=lambda: Probe('', problem='never holds'),
    fixes=dict.fromkeys(Platform, Fix('exit 3', automatic=True)),
  )
  prerequisites = (failing, _marked(tmp_path, 'later', automatic=True))

  assert check_host(prerequisites, Platform.UBUNTU, install=True) == 1

  assert not (tmp_path / 'later').exists()


def test_a_fix_whose_download_fails_inside_its_pipeline_stops_later_fixes(tmp_path):
  # a failed download hands its installer an empty script, which exits 0
  downloading = Prerequisite(
    name='downloading',
    purpose='a test prerequisite',
    probe=lambda: Probe('', problem='never holds'),
    fixes=dict.fromkeys(Platform, Fix('exit 22 | sh', automatic=True)),
  )
  prerequisites = (downloading, _marked(tmp_path, 'later', automatic=True))

  assert check_host(prerequisites, Platform.MACOS, install=True) == 1

  assert not (tmp_path / 'later').exists()


def test_every_prerequisite_has_a_fix_per_platform_and_requires_an_earlier_one():
  roster = prerequisites()
  for index, prerequisite in enumerate(roster):
    assert set(prerequisite.fixes) == set(Platform), prerequisite.name
    earlier = {item.name for item in roster[:index]}
    assert set(prerequisite.requires) <= earlier, prerequisite.name


@pytest.mark.parametrize(
  ('system', 'release', 'expected'),
  [
    ('darwin', None, Platform.MACOS),
    ('linux', {'ID': 'ubuntu'}, Platform.UBUNTU),
  ],
)
def test_the_host_platform_is_macos_or_ubuntu(monkeypatch, system, release, expected):
  monkeypatch.setattr(sys, 'platform', system)
  monkeypatch.setattr(host_check.platform, 'freedesktop_os_release', lambda: release)

  assert host_platform() is expected


def _no_os_release() -> dict[str, str]:
  raise OSError('no os-release')


@pytest.mark.parametrize(
  ('system', 'release'),
  [('linux', lambda: {'ID': 'debian'}), ('linux', _no_os_release), ('win32', _no_os_release)],
)
def test_another_host_platform_is_refused(monkeypatch, system, release):
  monkeypatch.setattr(sys, 'platform', system)
  monkeypatch.setattr(host_check.platform, 'freedesktop_os_release', release)

  with pytest.raises(UnsupportedHost, match='only macOS and Ubuntu'):
    host_platform()


def test_a_managed_session_is_refused(monkeypatch):
  monkeypatch.setenv(ISOLATION_ENV, 'boxed')

  with pytest.raises(UnsupportedHost, match='outside a managed session'):
    check_this_host(install=False)
