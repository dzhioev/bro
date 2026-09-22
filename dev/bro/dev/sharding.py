"""deal a pytest collection into shards, one run each.

`--shard K/N` keeps the K-th of N shards of the collected items. The unit dealt
is a test class, or a test function outside any class, so the tests a
class-level fixture serves stay in one run; a module- or session-scoped fixture
runs once per shard. Units are dealt round-robin in collection order, so every
item lands in exactly one shard.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import pytest

_SHARD_PATTERN = re.compile(r'([1-9][0-9]*)/([1-9][0-9]*)')


@dataclass(frozen=True)
class Shard:
  """the `index`-th of `count` shards, counted from 1."""

  index: int
  count: int

  def __post_init__(self) -> None:
    if not 1 <= self.index <= self.count:
      raise ValueError(f'shard {self.index}/{self.count} is outside 1..{self.count}')

  def __str__(self) -> str:
    return f'{self.index}/{self.count}'

  def holds(self, unit_number: int) -> bool:
    return unit_number % self.count == self.index - 1


_SHARD = pytest.StashKey[Shard]()


def parse_shard(text: str) -> Shard:
  match = _SHARD_PATTERN.fullmatch(text)
  if match is None:
    raise ValueError(f'a shard reads K/N with 1 <= K <= N, not {text!r}')
  return Shard(int(match.group(1)), int(match.group(2)))


def deal[T](items: Iterable[T], unit: Callable[[T], str], shard: Shard) -> tuple[list[T], list[T]]:
  """the items `shard` keeps and the ones it drops: units are numbered as they
  first appear and dealt round-robin."""
  numbers: dict[str, int] = {}
  kept: list[T] = []
  dropped: list[T] = []
  for item in items:
    number = numbers.setdefault(unit(item), len(numbers))
    (kept if shard.holds(number) else dropped).append(item)
  return kept, dropped


def unit_of(item: pytest.Item) -> str:
  """the fixture-sharing unit an item belongs to: its class, else the function itself."""
  if isinstance(item, pytest.Function):
    owner = item.cls.__qualname__ if item.cls is not None else item.originalname
    return f'{item.path}::{owner}'
  return item.nodeid


def pytest_addoption(parser: pytest.Parser) -> None:
  parser.addoption(
    '--shard',
    metavar='K/N',
    help='run the K-th of N shards of the collection, dealt by test class or standalone function',
  )


def pytest_configure(config: pytest.Config) -> None:
  text = config.getoption('shard')
  if text is None:
    return
  try:
    config.stash[_SHARD] = parse_shard(text)
  except ValueError as error:
    raise pytest.UsageError(str(error)) from error


def pytest_report_header(config: pytest.Config) -> list[str]:
  shard = config.stash.get(_SHARD, None)
  return [] if shard is None else [f'shard: {shard}']


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
  shard = config.stash.get(_SHARD, None)
  if shard is None:
    return
  kept, dropped = deal(items, unit_of, shard)
  if len(kept) == 0:
    raise pytest.UsageError(f'shard {shard} holds none of the collection: fewer units than shards')
  items[:] = kept
  config.hook.pytest_deselected(items=dropped)
