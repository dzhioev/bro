#!/usr/bin/env python

import base.args

from abc import abstractmethod, ABC
import sys
import asyncio
from functools import cached_property
from typing import Callable


class Bro(ABC):
  @abstractmethod
  async def tell(self, phrase: str, images: list[str] | None) -> None: ...

  @abstractmethod
  async def ask(self) -> str: ...


class LazyConstants:
  @cached_property
  def BROS_BY_TYPE(self) -> dict[str, type[Bro]]:
    import bro.bros

    result = {}

    def register_bro(type: str, constructor: Callable[[], Bro]) -> None:
      assert type not in result
      result[type] = constructor

    register_bro('echo', bro.bros.EchoBro.create)
    register_bro('chat_gpt', bro.bros.ChatGPTBro.create)
    return result

  @cached_property
  def BROS_TYPES(self) -> list[str]:
    return list(self.BROS_BY_TYPE.keys())


LAZY_CONSTANTS: LazyConstants = LazyConstants()


def get_bro(type: str, *args, **kwargs) -> Bro:
  constructor = LAZY_CONSTANTS.BROS_BY_TYPE.get(type)
  if constructor is None:
    raise ValueError(f'Unknown bro type: {type}')
  return constructor(*args, **kwargs)


async def bro_main(request: str, bro_type: str, attachments: list[str], *args, **kwargs):
  bro = get_bro(bro_type, *args, **kwargs)
  print(f'> {request}')
  await bro.tell(request, attachments)
  print()
  asked = await bro.ask()
  print(f'< {asked}')


def main(argv) -> int | None:
  parser = base.args.Parser(description='Chat with bro')
  parser.add_argument('--attach', '-a', dest='attachments', nargs='*', default=[])
  parser.add_argument('--bro-type', '-t', choices=LAZY_CONSTANTS.BROS_TYPES, default='echo')
  parser.add_argument('request')
  return asyncio.run(bro_main(**parser.parse(argv)))


if __name__ == '__main__':
  sys.exit(main(sys.argv))
