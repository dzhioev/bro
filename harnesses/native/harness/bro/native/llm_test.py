import os
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

import pytest

from bro.inbox import Inbox
from bro.llm.wire import SystemPrompt, Text, UserMessage
from bro.native.llm import LLM


class _OneStepLLM(LLM):
  async def send(
    self, inputs: Sequence[SystemPrompt | UserMessage], *, request_timeout: Optional[float] = None
  ) -> str:
    del request_timeout
    message = inputs[-1]
    assert isinstance(message, UserMessage)
    text = message.content[0]
    assert isinstance(text, Text)
    await self._track_step('user_input', text.text)
    return 'done'


@pytest.mark.asyncio
async def test_a_step_touches_the_activity_file(tmp_path: Path):
  activity_file = tmp_path / 'activity'
  activity_file.touch()
  os.utime(activity_file, (1000.0, 1000.0))

  await _OneStepLLM(Inbox(), activity_file=activity_file).send([UserMessage((Text('hi'),))])

  assert activity_file.stat().st_mtime > 1000.0


def test_diagnostic_cli_requires_a_provider():
  from bro.native.llm import main

  with pytest.raises(SystemExit):
    main(['hello'])
