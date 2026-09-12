import os
import shutil
import subprocess
from pathlib import Path

INSTALLER = Path(__file__).parent / 'install_awscli.sh'
_SUPPORT_FILES = ('install_awscli.sh', 'prelude.sh', 'log.sh', 'strict.sh')
_SYSTEM_COMMANDS = (
  'basename',
  'cat',
  'chmod',
  'date',
  'dirname',
  'mkdir',
  'mktemp',
  'rm',
)


def _write_command(directory: Path, name: str, body: str) -> None:
  command = directory / name
  command.write_text(f'#!/bin/bash\n{body}')
  command.chmod(0o755)


def _prepare(
  tmp_path: Path, platform: str, user_id: int, *, with_sudo: bool = True
) -> dict[str, str]:
  scripts = tmp_path / 'scripts'
  scripts.mkdir()
  for name in _SUPPORT_FILES:
    shutil.copy2(INSTALLER.parent / name, scripts / name)

  binaries = tmp_path / 'bin'
  binaries.mkdir()
  for name in _SYSTEM_COMMANDS:
    target = shutil.which(name)
    assert target is not None
    (binaries / name).symlink_to(target)

  _write_command(
    binaries,
    'uname',
    f'if [ "$1" = "-s" ]; then echo {platform}; else echo x86_64; fi\n',
  )
  _write_command(binaries, 'id', f'echo {user_id}\n')
  _write_command(binaries, 'curl', 'printf "%s\\n" "$*" >> "$CALL_LOG"\nprintf archive > "${!#}"\n')
  _write_command(
    binaries,
    'unzip',
    'mkdir -p "$4/aws"\n'
    'cat > "$4/aws/install" <<\'EOF\'\n'
    '#!/bin/bash\n'
    'printf "install\\n" >> "$CALL_LOG"\n'
    'EOF\n'
    'chmod +x "$4/aws/install"\n',
  )
  _write_command(binaries, 'brew', 'printf "brew %s\\n" "$*" >> "$CALL_LOG"\n')
  if with_sudo:
    _write_command(
      binaries,
      'sudo',
      'printf "sudo %s\\n" "$*" >> "$CALL_LOG"\n"$@"\n',
    )

  return {
    **os.environ,
    'PATH': str(binaries),
    'CALL_LOG': str(tmp_path / 'calls.log'),
    'BRO_LOG_LEVEL': 'ERROR',
    'TMPDIR': str(tmp_path),
    'INSTALLER': str(scripts / 'install_awscli.sh'),
  }


def _run_installer(
  environment: dict[str, str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
  return subprocess.run(
    ['/bin/bash', '-e', environment['INSTALLER']],
    env=environment,
    capture_output=True,
    text=True,
    check=check,
  )


def test_linux_root_installs_the_official_bundle_without_sudo(tmp_path):
  environment = _prepare(tmp_path, 'Linux', 0)

  _run_installer(environment)

  download, install = (tmp_path / 'calls.log').read_text().splitlines()
  assert download.startswith('-fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o ')
  assert download.endswith('/awscli.zip')
  assert install == 'install'
  assert not Path(download.rsplit(' ', 1)[1]).parent.exists()


def test_linux_user_runs_the_bundle_installer_through_sudo(tmp_path):
  environment = _prepare(tmp_path, 'Linux', 1000)

  _run_installer(environment)

  calls = (tmp_path / 'calls.log').read_text().splitlines()
  assert calls[0].startswith('-fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o ')
  assert calls[1].startswith('sudo ')
  assert calls[2] == 'install'


def test_linux_user_without_sudo_fails_before_downloading(tmp_path):
  environment = _prepare(tmp_path, 'Linux', 1000, with_sudo=False)

  result = _run_installer(environment, check=False)

  assert result.returncode != 0
  assert 'requires root or sudo' in result.stderr
  assert not (tmp_path / 'calls.log').exists()


def test_macos_installs_through_homebrew(tmp_path):
  environment = _prepare(tmp_path, 'Darwin', 1000)

  _run_installer(environment)

  assert (tmp_path / 'calls.log').read_text().splitlines() == ['brew install awscli']
