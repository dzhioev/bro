"""A native run whose model starts a background job, then ends its turn without a word."""

import json
import shlex
from pathlib import Path
from typing import ClassVar

import bro.mcp as mcp
from bro.bro import BaseBro
from bro.llm.observer import InterimAssistantTextEvent, ObservedEvent, Observer

JOB_TOOL = 'bro__job'


class ShellBro(BaseBro):
  name = 'silent-turn-shell'
  description = 'a bare bro with a shell'
  tools: ClassVar = [mcp.brash(mcp.ANY)]

  def __init__(self):
    super().__init__(system_prompt='')


def job_arguments(release: Path) -> str:
  """A background job that runs until `release` exists."""
  command = f'while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.01; done'
  return json.dumps({'command': command, 'mode': 'bg'})


class ReleaseWhenIdle(Observer):
  """Creates `release` once a turn ends silently and the run waits on live work."""

  def __init__(self, release: Path):
    self.release = release

  def on_event(self, event: ObservedEvent) -> None:
    # The runner surfaces an unsettled turn's reply as interim text just before it idles.
    if isinstance(event, InterimAssistantTextEvent) and event.content == '':
      self.release.touch()
