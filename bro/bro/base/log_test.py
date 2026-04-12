#!/usr/bin/env python
import subprocess
import sys


def run_log_script(code: str) -> str:
  result = subprocess.run(
    [sys.executable, '-c', code],
    capture_output=True,
    text=True,
  )
  return result.stderr


class TestLogLevel:
  def test_info_visible_by_default(self):
    output = run_log_script('from base import log; log.info("hello")')
    assert 'hello' in output

  def test_debug_hidden_by_default(self):
    output = run_log_script('from base import log; log.debug("secret")')
    assert 'secret' not in output

  def test_warning_visible_by_default(self):
    output = run_log_script('from base import log; log.warning("warn")')
    assert 'warn' in output

  def test_debug_visible_after_set_level(self):
    output = run_log_script(
      'import logging; from base import log; log.set_level(logging.DEBUG); log.debug("verbose")'
    )
    assert 'verbose' in output

  def test_info_hidden_after_set_level_warning(self):
    output = run_log_script(
      'import logging; from base import log; log.set_level(logging.WARNING); log.info("quiet")'
    )
    assert 'quiet' not in output


class TestLogFormat:
  def test_format_contains_level_and_logger_name(self):
    output = run_log_script('from base import log; log.info("test123")')
    assert 'INFO[ppp]' in output
    assert 'test123' in output


class TestThirdPartyIsolation:
  def test_third_party_info_not_visible(self):
    output = run_log_script(
      'from base import log; import logging; logging.getLogger("urllib3").info("noisy")'
    )
    assert 'noisy' not in output

  def test_third_party_warning_visible(self):
    output = run_log_script(
      'from base import log; import logging; logging.getLogger("urllib3").warning("important")'
    )
    assert 'important' in output
