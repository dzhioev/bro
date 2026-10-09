import subprocess

import pytest

from bro import brash_policy, mcp
from bro.brash import REFUSED_STATUS, Policy


@pytest.mark.parametrize(
  ('reach', 'writable'),
  [
    (mcp.Reach(brash=mcp.Brash(commands=('git log ...',))), False),
    (mcp.Reach(files=mcp.Files(write=False), brash=mcp.Brash(commands=('git log ...',))), False),
    (mcp.Reach(files=mcp.Files(), brash=mcp.Brash(commands=('git log ...',))), True),
  ],
)
def test_a_finite_command_list_writes_a_policy_with_the_reachs_files_level(
  tmp_path, reach, writable
):
  path = brash_policy.write(tmp_path, reach)

  assert path is not None
  assert Policy.read(path) == Policy(entries=('git log ...',), writable=writable)


@pytest.mark.parametrize(
  'reach', [mcp.Reach(), mcp.Reach(files=mcp.Files(), brash=mcp.Brash(unrestricted=True))]
)
def test_a_reach_without_a_finite_command_list_writes_no_policy(tmp_path, reach):
  assert brash_policy.write(tmp_path, reach) is None
  assert list(tmp_path.iterdir()) == []


def test_a_line_starts_in_brash_under_a_policy_and_in_bash_without_one(tmp_path):
  policy = brash_policy.write(tmp_path, mcp.Reach(brash=mcp.Brash(commands=('printf ...',))))
  line = 'echo $((1 + 1))'

  in_bash = subprocess.run(brash_policy.line_argv(line, None), capture_output=True, text=True)
  in_brash = subprocess.run(brash_policy.line_argv(line, policy), capture_output=True, text=True)

  assert (in_bash.returncode, in_bash.stdout) == (0, '2\n')
  assert in_brash.returncode == REFUSED_STATUS
  assert in_brash.stderr.startswith("brash: refused '$((1 + 1))'")


def test_the_published_policy_is_the_one_its_environment_names(monkeypatch, tmp_path):
  monkeypatch.delenv(brash_policy.POLICY_ENV, raising=False)
  with pytest.raises(RuntimeError, match=brash_policy.POLICY_ENV):
    brash_policy.published()

  monkeypatch.setenv(brash_policy.POLICY_ENV, str(tmp_path / 'policy.json'))
  assert brash_policy.published() == tmp_path / 'policy.json'
