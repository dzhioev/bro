import shlex
import subprocess
from pathlib import Path

_DEPLOY_LIBRARY = Path(__file__).parent / 'infra' / 'deploy_lib.sh'
_COMMIT = '0123456789abcdef0123456789abcdef01234567'
_LOCKED_COMMIT = 'fedcba9876543210fedcba9876543210fedcba98'


def _run_bash(body: str) -> subprocess.CompletedProcess[str]:
  return subprocess.run(
    ['bash', '-c', f'set -e\nsource {shlex.quote(str(_DEPLOY_LIBRARY))}\n{body}'],
    check=False,
    capture_output=True,
    text=True,
  )


def test_ecr_uri_uses_caller_supplied_repository_and_region():
  result = _run_bash(
    """
    aws() {
      [ "$*" = "sts get-caller-identity --query Account --output text" ] || return 88
      echo 123456789012
    }
    ecr_uri repository-a region-1
    """
  )
  assert result.returncode == 0
  assert result.stdout == '123456789012.dkr.ecr.region-1.amazonaws.com/repository-a\n'


def test_trigger_image_build_reuses_the_commit_tag():
  result = _run_bash(
    f"""
    git() {{
      [ "$*" = "rev-parse HEAD" ] || return 88
      echo {_COMMIT}
    }}
    aws() {{
      [[ " $* " == *" ecr list-images "* ]] || return 89
      [[ " $* " == *" --region region-1 "* ]] || return 90
      [[ " $* " == *" --repository-name repository-a "* ]] || return 91
      echo sha256:cached
    }}
    trigger_image_build target-a repository-a project-a region-1
    """
  )
  assert result.returncode == 0
  assert result.stdout == (
    f'using existing image for target target-a, commit {_COMMIT}: sha256:cached\n'
  )


def test_trigger_image_build_starts_the_caller_supplied_project():
  result = _run_bash(
    f"""
    git() {{
      case "$1 $2" in
        "rev-parse HEAD") echo {_COMMIT} ;;
        "fetch origin") ;;
        "for-each-ref --format=%(objectname)") echo {_COMMIT} ;;
        "merge-base --is-ancestor") ;;
        *) return 88 ;;
      esac
    }}
    aws() {{
      if [[ " $* " == *" ecr list-images "* ]]; then
        echo None
      elif [[ " $* " == *" codebuild start-build "* ]]; then
        [[ " $* " == *" --project-name project-a "* ]] || return 90
        echo project-a:build-id
      elif [[ " $* " == *" codebuild batch-get-builds "* ]]; then
        echo "SUCCEEDED None"
      else
        return 89
      fi
    }}
    sleep() {{ :; }}
    trigger_image_build target-a repository-a project-a region-1
    """
  )
  assert result.returncode == 0
  assert f'started build project-a:build-id (target target-a, commit {_COMMIT})\n' in result.stdout
  assert result.stdout.endswith('build project-a:build-id: SUCCEEDED\n')


def test_stage_bro_wheel_builds_the_framework_working_tree(tmp_path):
  (tmp_path / 'pyproject.toml').write_text('[project]\nname = "bro"\n')
  (tmp_path / 'bro').mkdir()
  arguments = tmp_path / 'arguments'
  result = _run_bash(
    f"""
    git() {{
      [ "$*" = "rev-parse --show-toplevel" ] || return 88
      echo {shlex.quote(str(tmp_path))}
    }}
    uv() {{
      echo "$*" > {shlex.quote(str(arguments))}
      local previous=""
      for argument in "$@"; do
        if [ "$previous" = "--out-dir" ]; then
          mkdir -p "$argument"
          touch "$argument/bro-0.1.0-py3-none-any.whl"
        fi
        previous="$argument"
      done
    }}
    stage_bro_wheel
    """
  )
  assert result.returncode == 0, result.stderr
  assert '--package bro' in arguments.read_text()
  assert result.stdout.endswith('from the framework working tree\n')


def test_stage_bro_wheel_builds_the_revision_in_a_consumer_lock(tmp_path):
  (tmp_path / 'pyproject.toml').write_text('[project]\nname = "consumer"\n')
  (tmp_path / 'uv.lock').write_text(
    f"""\
version = 1

[[package]]
name = "bro"
source = {{ git = "https://example.invalid/bro?branch=main#{_LOCKED_COMMIT}" }}
"""
  )
  arguments = tmp_path / 'arguments'
  result = _run_bash(
    f"""
    git() {{
      if [ "$*" = "rev-parse --show-toplevel" ]; then
        echo {shlex.quote(str(tmp_path))}
      elif [ "$1" = "clone" ]; then
        mkdir -p "${{@: -1}}"
      elif [ "$1" = "-C" ] && [ "$3" = "checkout" ]; then
        echo "$*" >> {shlex.quote(str(arguments))}
      else
        return 88
      fi
    }}
    uv() {{
      echo "$*" >> {shlex.quote(str(arguments))}
      local previous=""
      for argument in "$@"; do
        if [ "$previous" = "--out-dir" ]; then
          mkdir -p "$argument"
          touch "$argument/bro-0.1.0-py3-none-any.whl"
        fi
        previous="$argument"
      done
    }}
    stage_bro_wheel
    """
  )
  assert result.returncode == 0, result.stderr
  assert f'checkout --quiet --detach {_LOCKED_COMMIT}' in arguments.read_text()
  assert result.stdout.endswith(f'from https://example.invalid/bro@{_LOCKED_COMMIT}\n')
