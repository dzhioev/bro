#!/usr/bin/env python
import subprocess

import pytest

import bro.llm.usage as usage
from bro.launch.hold import HOLD_VARIABLE
from bro.workflow.co_author import append_trailer, strip_trailer, trailer

FOOTER = '> created with Claude Code 2.1 | Opus 4.8: ↑(1 0 0) ↓2'


@pytest.fixture
def repo(tmp_path, monkeypatch):
  """a checkout declaring no identity of its own, with the host's config out of
  reach so what the tests set is all git can see."""
  subprocess.run(['git', 'init', '-q', '-b', 'master', str(tmp_path)], check=True)
  monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(tmp_path / 'absent-global'))
  monkeypatch.setenv('GIT_CONFIG_SYSTEM', str(tmp_path / 'absent-system'))
  monkeypatch.chdir(tmp_path)
  return tmp_path


def _identity(repo, name: str, email: str) -> None:
  subprocess.run(['git', 'config', 'user.name', name], cwd=repo, check=True)
  subprocess.run(['git', 'config', 'user.email', email], cwd=repo, check=True)


def _session(monkeypatch, hold: str) -> None:
  monkeypatch.setenv(usage.SESSION_ID_VARIABLE, 'co-author-test-session')
  monkeypatch.setenv(HOLD_VARIABLE, hold)


class TestTrailer:
  @pytest.mark.parametrize('hold', ['detached', 'attended', 'guided'])
  def test_an_interactive_session_credits_the_human(self, hold, repo, monkeypatch):
    _identity(repo, 'Ada Lovelace', 'ada@example.com')
    _session(monkeypatch, hold)
    assert trailer() == 'Co-Authored-By: Ada Lovelace <ada@example.com>'

  def test_an_unattended_session_credits_nobody(self, repo, monkeypatch):
    _identity(repo, 'Ada Lovelace', 'ada@example.com')
    _session(monkeypatch, 'unattended')
    assert trailer() is None

  def test_a_human_shell_credits_nobody(self, repo, monkeypatch):
    _identity(repo, 'Ada Lovelace', 'ada@example.com')
    monkeypatch.delenv(usage.SESSION_ID_VARIABLE, raising=False)
    monkeypatch.delenv(usage.USAGE_FILE_VARIABLE, raising=False)
    monkeypatch.setenv(HOLD_VARIABLE, 'attended')
    assert trailer() is None

  def test_a_host_declaring_no_identity_credits_nobody(self, repo, monkeypatch):
    _session(monkeypatch, 'attended')
    assert trailer() is None

  def test_a_half_declared_identity_credits_nobody(self, repo, monkeypatch):
    subprocess.run(['git', 'config', 'user.name', 'Ada Lovelace'], cwd=repo, check=True)
    _session(monkeypatch, 'attended')
    assert trailer() is None

  def test_a_failing_git_stops_the_commit(self, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(tmp_path / 'absent-global'))
    monkeypatch.setenv('GIT_CONFIG_SYSTEM', str(tmp_path / 'absent-system'))
    (tmp_path / '.git').write_text('gitdir: nowhere\n')
    _session(monkeypatch, 'attended')
    with pytest.raises(RuntimeError, match='git config --get user.name failed'):
      trailer()


class TestMessageEditing:
  def test_the_trailer_lands_after_the_footer(self):
    message = append_trailer(f'subject\n\nbody\n\n{FOOTER}\n', 'Co-Authored-By: Ada <ada@e.com>')
    assert message == f'subject\n\nbody\n\n{FOOTER}\n\nCo-Authored-By: Ada <ada@e.com>\n'

  def test_appending_replaces_a_trailer_already_there(self):
    once = append_trailer('subject\n', 'Co-Authored-By: Ada <ada@e.com>')
    assert append_trailer(once, 'Co-Authored-By: Ada <ada@e.com>') == once

  def test_stripping_leaves_the_message_and_its_footer(self):
    message = f'subject\n\nbody\n\n{FOOTER}\n\nCo-Authored-By: Ada <ada@e.com>\n'
    assert strip_trailer(message) == f'subject\n\nbody\n\n{FOOTER}'

  def test_stripping_a_message_without_one_is_a_no_op(self):
    assert strip_trailer('subject\n\nbody\n') == 'subject\n\nbody'
