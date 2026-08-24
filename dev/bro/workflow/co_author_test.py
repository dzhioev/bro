#!/usr/bin/env python
import pytest

import bro.llm.usage as usage
from bro.launch.hold import HOLD_VARIABLE
from bro.workflow.co_author import append_trailer, strip_trailer, trailer
from bro.workspace.human import HUMAN_EMAIL_ENV, HUMAN_NAME_ENV

FOOTER = '> created with Claude Code 2.1 | Opus 4.8: ↑(1 0 0) ↓2'


@pytest.fixture
def human(monkeypatch):
  monkeypatch.setenv(HUMAN_NAME_ENV, 'Ada Lovelace')
  monkeypatch.setenv(HUMAN_EMAIL_ENV, 'ada@example.com')


def _session(monkeypatch, hold: str) -> None:
  monkeypatch.setenv(usage.SESSION_ID_VARIABLE, 'co-author-test-session')
  monkeypatch.setenv(HOLD_VARIABLE, hold)


class TestTrailer:
  @pytest.mark.parametrize('hold', ['detached', 'attended', 'guided'])
  def test_an_interactive_session_credits_the_human(self, hold, human, monkeypatch):
    _session(monkeypatch, hold)
    assert trailer() == 'Co-Authored-By: Ada Lovelace <ada@example.com>'

  def test_an_unattended_session_credits_nobody(self, human, monkeypatch):
    _session(monkeypatch, 'unattended')
    assert trailer() is None

  def test_a_human_shell_credits_nobody(self, human, monkeypatch):
    monkeypatch.delenv(usage.SESSION_ID_VARIABLE, raising=False)
    monkeypatch.delenv(usage.USAGE_FILE_VARIABLE, raising=False)
    monkeypatch.setenv(HOLD_VARIABLE, 'attended')
    assert trailer() is None

  def test_a_launch_naming_no_human_credits_nobody(self, monkeypatch):
    _session(monkeypatch, 'attended')
    assert trailer() is None


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
