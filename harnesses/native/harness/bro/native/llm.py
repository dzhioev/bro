#!/usr/bin/env python

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

import bro.base.args as base_args
from bro.base.offload import off_loop
from bro.inbox import Inbox
from bro.llm import wires
from bro.llm.mcp import MCPServer, ToolRegistry
from bro.llm.observer import NullObserver, Observer
from bro.llm.tracker import NullTracker, StepKind, Tracker
from bro.llm.wire import File, Prefix, SystemPrompt, Text, UserMessage


class LLM(ABC):
  def __init__(
    self,
    inbox: Inbox,
    mcp_servers: Optional[list[MCPServer]] = None,
    observer: Optional[Observer] = None,
    tracker: Optional[Tracker] = None,
    agent: Optional[str] = None,
    activity_file: Optional[Path] = None,
  ):
    self.inbox = inbox
    self.tools = ToolRegistry(mcp_servers if mcp_servers is not None else [])
    self.observer: Observer = observer if observer is not None else NullObserver()
    self.tracker: Tracker = tracker if tracker is not None else NullTracker()
    self.agent = agent
    self.activity_file = activity_file

  @abstractmethod
  async def send(
    self, inputs: Sequence[SystemPrompt | UserMessage], *, request_timeout: Optional[float] = None
  ) -> str: ...

  async def wake(self, *, request_timeout: Optional[float] = None) -> str:
    raise NotImplementedError

  def restore(self, prefix: Prefix) -> None:
    raise NotImplementedError

  def seed_chain(self, response_id: str) -> None:
    raise NotImplementedError

  async def _track_step(self, kind: StepKind, body: object, **extras: object) -> Optional[int]:
    """record one step of the loop, touching the activity mark when one was given."""
    if self.activity_file is not None:
      self.activity_file.touch()
    return await off_loop(self.tracker.step, kind, body, **extras)

  def cumulative_usage(self) -> Optional[dict[str, dict[str, int]]]:
    """per-model counts in the four billed token classes (`bro.llm.usage.CLASSES`),
    summed over this instance's lifetime; None when the provider doesn't
    track usage."""
    return None


async def llm_main(request: str, llm: str, attachments: list[str]):
  from bro.llm.providers import bind_recipe
  from bro.native import loop

  spec = bind_recipe(llm)
  if spec.WIRE not in wires.client_names():
    raise ValueError(f'provider {spec.TYPE!r} wire {spec.WIRE!r} has no client')
  instance = loop.create(spec, Inbox())
  inputs = [UserMessage((Text(request), *(File.from_path(path) for path in attachments)))]
  print(f'> {request}')
  print()
  response = await instance.send(inputs)
  print(f'< {response}')


def main(argv: list[str]) -> Optional[int]:
  from bro.launch.llm_flags import add_llm_flags

  parser = base_args.Parser(description='chat with LLM')
  parser.add_argument('--attach', '-a', dest='attachments', nargs='*', default=[])
  add_llm_flags(parser, required=True)
  parser.add_argument('request')
  return asyncio.run(llm_main(**parser.parse(argv)))
