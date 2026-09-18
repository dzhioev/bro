from typing import get_args

import bro.mcp as mcp
from bros.analyst import Analyst


def test_analyst_declares_an_unrestricted_shell_on_both_harnesses():
  for harness in get_args(mcp.Harness):
    assert Analyst()._selected_tools_for(harness).shell_unrestricted is True
