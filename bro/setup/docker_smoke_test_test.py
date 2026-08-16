import os
import re
import subprocess
from pathlib import Path

HELPER_SCRIPT = Path(__file__).parent / 'docker_smoke_test.sh'

_STUB_OCI_COMMAND = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$OCI_LOG"
if [ "$1" = create ]; then
  echo smoke-cid
fi
"""


def _prepare(tmp_path: Path) -> dict[str, str]:
  binaries = tmp_path / 'bin'
  binaries.mkdir()
  stub_oci = binaries / 'stub-oci'
  stub_oci.write_text(_STUB_OCI_COMMAND)
  stub_oci.chmod(0o755)
  stub_preparer = binaries / 'stub-prepare'
  stub_preparer.write_text('#!/usr/bin/env bash\n')
  stub_preparer.chmod(0o755)
  return {
    **os.environ,
    'PATH': f'{binaries}:{os.environ["PATH"]}',
    'OCI_LOG': str(tmp_path / 'oci.log'),
    'SMOKE_HELPER': str(HELPER_SCRIPT),
  }


def _run_smoke(body: str, tmp_path: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
  return subprocess.run(
    ['bash', '-e', '-c', f'source "$SMOKE_HELPER" stub-oci stub-prepare\n{body}'],
    cwd=tmp_path,
    env=_prepare(tmp_path),
    capture_output=True,
    text=True,
    check=check,
  )


def _oci_calls(tmp_path: Path) -> list[str]:
  log = tmp_path / 'oci.log'
  return log.read_text().splitlines() if log.exists() else []


def _built_image(build_call: str) -> str:
  built = re.fullmatch(r'build -f Dockerfile -t (smoke-test-\d+) \.', build_call)
  assert built is not None
  return built.group(1)


def test_copies_land_between_container_create_and_start(tmp_path):
  (tmp_path / 'Dockerfile').write_text('FROM scratch\n')
  (tmp_path / 'config.json').write_text('{}\n')
  (tmp_path / 'seed.sql').write_text('select 1;\n')

  _run_smoke(
    """
    smoke_build Dockerfile
    smoke_copy config.json /etc/app/config.json
    smoke_copy seed.sql /var/seed.sql
    smoke_start 8080 -e KEY=VAL
    """,
    tmp_path,
  )

  build, create, *rest = _oci_calls(tmp_path)
  image = _built_image(build)
  assert re.fullmatch(rf'create -p \d+:8080 -e KEY=VAL {image}', create)
  assert rest == [
    'cp config.json smoke-cid:/etc/app/config.json',
    'cp seed.sql smoke-cid:/var/seed.sql',
    'start smoke-cid',
    'rm -f smoke-cid',
    f'rmi -f {image}',
  ]


def test_absent_copy_source_fails_before_the_container_exists(tmp_path):
  result = _run_smoke('smoke_copy missing.json /etc/app/config.json', tmp_path, check=False)

  assert result.returncode != 0
  assert 'missing.json' in result.stderr
  assert _oci_calls(tmp_path) == []


def test_image_is_removed_when_the_run_fails_before_the_container(tmp_path):
  (tmp_path / 'Dockerfile').write_text('FROM scratch\n')

  result = _run_smoke(
    """
    smoke_build Dockerfile
    smoke_copy missing.json /etc/app/config.json
    smoke_start 8080
    """,
    tmp_path,
    check=False,
  )

  assert result.returncode != 0
  build, *rest = _oci_calls(tmp_path)
  assert rest == [f'rmi -f {_built_image(build)}']
