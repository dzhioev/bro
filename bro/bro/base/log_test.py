#!/usr/bin/env python
import os
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
  def test_scope_is_main_for_inline_script(self):
    output = run_log_script('from base import log; log.info("test123")')
    assert 'INFO[main]' in output
    assert 'test123' in output

  def test_scope_is_filename_for_script(self):
    import tempfile

    with tempfile.NamedTemporaryFile(suffix='.py', mode='w', delete=False) as f:
      f.write('import sys; sys.path.insert(0, ".")\nfrom base import log; log.info("hello")')
      f.flush()
      result = subprocess.run([sys.executable, f.name], capture_output=True, text=True)
    name = os.path.splitext(os.path.basename(f.name))[0]
    assert f'INFO[{name}]' in result.stderr

  def test_scope_is_module_name(self):
    output = run_log_script(
      'import base.log_test_helper'
    )
    assert 'INFO[base.log_test_helper]' in output


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
