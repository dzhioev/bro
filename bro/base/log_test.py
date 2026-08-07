#!/usr/bin/env python
import os
import subprocess
import sys
from typing import Optional

import pytest

from bro.base import log


def run_log_script(code: str, level_env: Optional[str] = None) -> str:
  env = {k: v for k, v in os.environ.items() if k != log.LEVEL_ENV}
  if level_env is not None:
    env[log.LEVEL_ENV] = level_env
  result = subprocess.run(
    [sys.executable, '-c', code],
    capture_output=True,
    text=True,
    env=env,
  )
  return result.stderr


class TestLogLevel:
  def test_info_visible_by_default(self):
    output = run_log_script('from bro.base import log; log.info("hello")')
    assert 'hello' in output

  def test_debug_hidden_by_default(self):
    output = run_log_script('from bro.base import log; log.debug("secret")')
    assert 'secret' not in output

  def test_verbose_hidden_by_default(self):
    output = run_log_script('from bro.base import log; log.verbose("detail")')
    assert 'detail' not in output

  def test_warning_visible_by_default(self):
    output = run_log_script('from bro.base import log; log.warning("warn")')
    assert 'warn' in output

  def test_debug_visible_after_set_level(self):
    output = run_log_script(
      'import logging; from bro.base import log; log.set_level(logging.DEBUG); log.debug("verbose")'
    )
    assert 'verbose' in output

  def test_verbose_visible_after_set_level(self):
    output = run_log_script(
      'from bro.base import log; log.set_level(log.VERBOSE); log.verbose("detail")'
    )
    assert 'VERBOSE[main] detail' in output

  def test_verbose_level_hides_debug(self):
    output = run_log_script(
      'from bro.base import log; log.set_level(log.VERBOSE); log.debug("secret")'
    )
    assert 'secret' not in output

  def test_info_hidden_after_set_level_warning(self):
    output = run_log_script(
      'import logging; from bro.base import log; log.set_level(logging.WARNING); log.info("quiet")'
    )
    assert 'quiet' not in output


class TestLevelEnv:
  def test_env_sets_initial_level(self):
    output = run_log_script('from bro.base import log; log.verbose("detail")', level_env='VERBOSE')
    assert 'detail' in output

  def test_env_accepts_lowercase(self):
    output = run_log_script('from bro.base import log; log.info("quiet")', level_env='warning')
    assert 'quiet' not in output

  def test_unknown_env_level_raises(self):
    output = run_log_script('from bro.base import log', level_env='CHATTY')
    assert 'unknown log level' in output

  def test_set_level_exports_env(self):
    output = run_log_script(
      'import os; from bro.base import log; log.set_level(log.VERBOSE); '
      f'print(os.environ["{log.LEVEL_ENV}"], file=__import__("sys").stderr)'
    )
    assert 'VERBOSE' in output


class TestLevelNumber:
  def test_maps_names(self):
    assert log.level_number('verbose') == log.VERBOSE
    assert log.level_number('INFO') == 20

  def test_unknown_name_raises(self):
    with pytest.raises(ValueError):
      log.level_number('chatty')


class TestLogFormat:
  def test_scope_is_main_for_inline_script(self):
    output = run_log_script('from bro.base import log; log.info("test123")')
    assert 'INFO[main]' in output
    assert 'test123' in output

  def test_scope_is_filename_for_script(self):
    import tempfile

    with tempfile.NamedTemporaryFile(suffix='.py', mode='w', delete=False) as f:
      f.write('from bro.base import log; log.info("hello")')
      f.flush()
      result = subprocess.run([sys.executable, f.name], capture_output=True, text=True)
    name = os.path.splitext(os.path.basename(f.name))[0]
    assert f'INFO[{name}]' in result.stderr

  def test_scope_is_module_name(self):
    output = run_log_script('import bro.base.log_test_helper')
    assert 'INFO[bro.base.log_test_helper]' in output


class TestThirdPartyIsolation:
  def test_third_party_info_not_visible(self):
    output = run_log_script(
      'from bro.base import log; import logging; logging.getLogger("urllib3").info("noisy")'
    )
    assert 'noisy' not in output

  def test_third_party_warning_visible(self):
    output = run_log_script(
      'from bro.base import log; import logging; logging.getLogger("urllib3").warning("important")'
    )
    assert 'important' in output
