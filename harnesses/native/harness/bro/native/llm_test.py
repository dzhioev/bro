import os
from pathlib import Path
from typing import Optional

import pytest

from bro.inbox import Inbox
from bro.native.llm import LLM


class _OneStepLLM(LLM):
  async def send(self, messages: list[dict], *, request_timeout: Optional[float] = None) -> str:
    del request_timeout
    await self._track_step('user_input', messages[-1]['content'])
    return 'done'


@pytest.mark.asyncio
async def test_a_step_touches_the_activity_file(tmp_path: Path):
  activity_file = tmp_path / 'activity'
  activity_file.touch()
  os.utime(activity_file, (1000.0, 1000.0))

  await _OneStepLLM(Inbox(), activity_file=activity_file).send([{'role': 'user', 'content': 'hi'}])

  assert activity_file.stat().st_mtime > 1000.0
