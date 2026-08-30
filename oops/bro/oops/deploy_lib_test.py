import os
import re
import shlex
import subprocess
from pathlib import Path

from bro.base.spawn import console_script
from bro.oops.targets import PLAN_UNSAFE_EXIT_CODE

_DEPLOY_LIBRARY = Path(__file__).parent / 'infra' / 'deploy_lib.sh'
_COMMIT = '0123456789abcdef0123456789abcdef01234567'
_LOCKED_COMMIT = 'fedcba9876543210fedcba9876543210fedcba98'


def _run_bash(body: str) -> subprocess.CompletedProcess[str]:
  scripts = Path(console_script('bro-oops-dir')).parent
  return subprocess.run(
    ['bash', '-c', f'set -e\nsource {shlex.quote(str(_DEPLOY_LIBRARY))}\n{body}'],
    check=False,
    capture_output=True,
    text=True,
    env={**os.environ, 'PATH': os.pathsep.join((str(scripts), os.environ['PATH']))},
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


def test_cdk_diff_passes_a_change_with_no_destructive_impact():
  result = _run_bash(
    """
    npx() {
      printf 'Stack StackA\\nResources\\n[~] AWS::ECS::Service Service ServiceD69\\n'
      printf '[+] AWS::S3::Bucket Bucket B1\\n'
      printf '[+] Fn::ForEach::Buckets (expands to 2 resources at deploy time)\\n'
    }
    cdk_diff . StackA
    """
  )
  assert result.returncode == 0, result.stderr
  assert '[~] AWS::ECS::Service Service ServiceD69\n' in result.stdout
  assert result.stderr == ''


# the CDK CLI ends a changed resource's line with one of these four words for every impact
# that costs the live resource, and prints nothing machine-readable beside them. A resource
# the deploy only updates ends on its logical id, whatever that id happens to be named.
_IMPACT_LINES = (
  '[~] AWS::ECS::Service Service ServiceD69 replace',
  '[-] AWS::SSM::Parameter Token TokenA1 destroy',
  '[-] AWS::DynamoDB::Table Trails TableB2 orphan',
  '[~] AWS::EC2::Volume Data VolumeC3 may be replaced',
  '[~] AWS::ECS::Cluster Cluster ClusterE5 replace (OR move to OtherStack.Res via refactoring)',
)
_UPDATE_LINES = (
  '[~] AWS::ECS::Service replace ServiceA1',
  '[~] AWS::Lambda::Function orphan OrphanF6',
)


def test_cdk_diff_reports_every_impact_the_cli_can_print():
  printed = '\\n'.join(('Stack StackA', 'Resources', *_IMPACT_LINES))
  result = _run_bash(
    f"""
    npx() {{
      printf '{printed}\\n'
    }}
    cdk_diff . StackA
    """
  )
  assert result.returncode == PLAN_UNSAFE_EXIT_CODE
  for line in _IMPACT_LINES:
    assert f'{line}\n' in result.stdout
    assert f'{line}\n' in result.stderr


# a `Fn::ForEach::` entry is rendered by a formatter that states no impact, so a loop the
# deploy removes or rewrites carries no verdict for the resources it expands to
_LOOP_LINES = (
  '[-] Fn::ForEach::Buckets (expands to dynamic count at deploy time)',
  '[~] Fn::ForEach::Tables (expands to 3 resources at deploy time)',
)


def test_cdk_diff_flags_a_changed_resource_loop_the_cli_leaves_unjudged():
  printed = '\\n'.join(('Stack StackA', 'Resources', *_LOOP_LINES))
  result = _run_bash(
    f"""
    npx() {{
      printf '{printed}\\n'
      printf '    Loop variable: Buckets\\n'
    }}
    cdk_diff . StackA
    """
  )
  assert result.returncode == PLAN_UNSAFE_EXIT_CODE
  for line in _LOOP_LINES:
    assert f'{line}\n' in result.stderr
  assert 'Loop variable' not in result.stderr


def test_cdk_diff_ignores_nested_property_annotations():
  result = _run_bash(
    """
    npx() {
      printf 'Stack StackA\\nResources\\n[~] AWS::ECS::Service Service ServiceD69 replace\\n'
      printf ' \\xe2\\x94\\x94\\xe2\\x94\\x80 [~] TaskDefinition (requires replacement)\\n'
    }
    cdk_diff . StackA
    """
  )
  assert result.returncode == PLAN_UNSAFE_EXIT_CODE
  assert 'TaskDefinition' not in result.stderr


def test_cdk_diff_flags_a_logical_id_the_cli_renders_like_an_impact():
  result = _run_bash(
    """
    npx() { printf 'Stack StackA\\nResources\\n[~] AWS::S3::Bucket Bucket destroy\\n'; }
    cdk_diff . StackA
    """
  )
  assert result.returncode == PLAN_UNSAFE_EXIT_CODE


def test_cdk_diff_reads_the_impact_field_rather_than_the_whole_line():
  printed = '\\n'.join(('Stack StackA', 'Resources', *_UPDATE_LINES))
  result = _run_bash(
    f"""
    npx() {{ printf '{printed}\\n'; }}
    cdk_diff . StackA
    """
  )
  assert result.returncode == 0, result.stderr
  assert result.stderr == ''


def test_plan_and_deploy_run_one_pinned_cdk_cli():
  result = _run_bash(
    """
    npx() { printf 'npx %s\\n' "$*"; }
    cdk_deploy . StackA
    cdk_diff . StackA
    """
  )
  assert result.returncode == 0, result.stderr
  invocations = [line for line in result.stdout.splitlines() if line.startswith('npx ')]
  packages = {line.split(' --package ')[1].split(' ')[0] for line in invocations}
  assert len(invocations) == 2
  assert len(packages) == 1
  assert re.fullmatch(r'aws-cdk@\d+\.\d+\.\d+', packages.pop()) is not None


def test_cdk_diff_refuses_an_option_that_would_narrow_the_diff():
  result = _run_bash(
    """
    npx() { printf 'npx %s\\n' "$*"; }
    cdk_diff . StackA --security-only
    """
  )
  assert result.returncode == 2
  assert 'stack names only' in result.stderr
  assert result.stdout == ''


def test_cdk_diff_fails_rather_than_diffing_from_the_caller_directory():
  result = _run_bash(
    """
    npx() { printf 'Stack StackA\\nResources\\n'; }
    cdk_diff /definitely/absent StackA
    """
  )
  assert result.returncode != 0
  assert result.stdout == ''


def test_cdk_diff_asks_cloudformation_for_the_verdict():
  result = _run_bash(
    """
    npx() { printf 'npx %s\\n' "$*"; }
    cdk_diff . StackA
    """
  )
  assert result.returncode == 0, result.stderr
  assert '--method=change-set' in result.stdout


def test_cdk_diff_reports_a_failing_cdk_over_an_impact_the_scan_saw():
  result = _run_bash(
    """
    npx() {
      printf '[-] AWS::EC2::VPC Vpc VpcA1 destroy\\n'
      return 7
    }
    cdk_diff . StackA
    """
  )
  assert result.returncode == 7


def test_cdk_diff_never_passes_a_failing_cdk_off_as_an_unsafe_plan():
  result = _run_bash(
    f"""
    npx() {{
      echo 'no credentials' >&2
      return {PLAN_UNSAFE_EXIT_CODE}
    }}
    cdk_diff . StackA
    """
  )
  assert result.returncode not in (0, PLAN_UNSAFE_EXIT_CODE)


def test_cdk_diff_reports_a_failing_cdk_over_a_clean_scan():
  result = _run_bash(
    """
    npx() {
      echo 'no credentials' >&2
      return 7
    }
    cdk_diff . StackA
    """
  )
  assert result.returncode == 7
  assert 'no credentials\n' in result.stdout
