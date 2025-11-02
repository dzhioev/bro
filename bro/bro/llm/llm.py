#!/usr/bin/env python

import base.args

import abc
import sys
import asyncio

class Bro(abc.ABC):
    @abc.abstractmethod
    async def tell(self, phrase: str) -> None:
        pass

    @abc.abstractmethod
    async def ask(self) -> str:
        pass

class EchoBro(Bro):
    def __init__(self):
        self.phrase = None

    async def tell(self, phrase: str) -> None:
        self.pharse = phrase

    async def ask(self) -> str:
        return f'Hello, world!: "{self.phrase}"!'


def get_bro(type: str, *args, **kwargs) -> Bro:
    if type == 'echo':
      return EchoBro(*args, **kwargs)
    if type == 'chat_gpt':
      import bro.bros
      return bro.bros.ChatGPTBro.create(*args, **kwargs)
    raise ValueError(f'Unknown bro type: {type}')

async def bro_main(bro_type: str, *args, **kwargs):
  bro = get_bro(bro_type, *args, **kwargs)
  for _ in range(5):
    to_tell = f'ping {_}'
    print(f'> {to_tell}')
    await bro.tell(to_tell)
    print()
    asked = await bro.ask()
    print(f'< {asked}')
    print()

def main(argv) -> int | None:
  parser = base.args.Parser(description='Chat with bro')
  parser.add_argument('bro_type', nargs='?', default='echo')
  kwargs = parser.parse(argv)
  return asyncio.run(bro_main(**kwargs))

if __name__ == '__main__':
  sys.exit(main(sys.argv))
