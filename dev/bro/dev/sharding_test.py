import pytest

from bro.dev.sharding import Shard, deal, parse_shard

pytest_plugins = ['pytester']

_SUITE = """
import pytest


@pytest.fixture(scope='class')
def shared():
  return object()


class TestFirst:
  def test_a(self, shared):
    pass

  def test_b(self, shared):
    pass


class TestSecond:
  def test_c(self):
    pass


def test_lone():
  pass


@pytest.mark.parametrize('n', [1, 2])
def test_cases(n):
  pass
"""


@pytest.fixture
def suite(pytester, monkeypatch):
  monkeypatch.setenv('PYTEST_DISABLE_PLUGIN_AUTOLOAD', '1')
  pytester.makepyfile(suite_test=_SUITE)
  return pytester


@pytest.fixture
def collected(suite):
  def collect(*args):
    result = suite.runpytest('-p', 'bro.dev.sharding', '--collect-only', '-q', *args)
    return [line.split('::', 1)[1] for line in result.outlines if '::' in line]

  return collect


def test_a_shard_keeps_whole_classes_and_standalone_functions(collected):
  assert collected('--shard', '1/2') == ['TestFirst::test_a', 'TestFirst::test_b', 'test_lone']
  assert collected('--shard', '2/2') == ['TestSecond::test_c', 'test_cases[1]', 'test_cases[2]']


def test_the_shards_partition_the_collection(collected):
  whole = collected()
  dealt = [item for index in (1, 2, 3) for item in collected('--shard', f'{index}/3')]
  assert len(whole) == 6
  assert sorted(dealt) == sorted(whole)


def test_a_shard_holding_no_unit_is_refused(suite):
  result = suite.runpytest('-p', 'bro.dev.sharding', '--shard', '5/5')
  assert result.ret == pytest.ExitCode.USAGE_ERROR
  result.stderr.fnmatch_lines(['*shard 5/5 holds none of the collection*'])


def test_a_malformed_shard_is_refused_before_collection(suite):
  result = suite.runpytest('-p', 'bro.dev.sharding', '--shard', '3/2')
  assert result.ret == pytest.ExitCode.USAGE_ERROR
  result.stderr.fnmatch_lines(['*shard 3/2 is outside 1..2*'])


@pytest.mark.parametrize('text', ['0/3', '4/3', '1/0', '3', 'a/b', '1/3/', ' 1/3'])
def test_parse_shard_refuses_anything_but_k_of_n(text):
  with pytest.raises(ValueError):
    parse_shard(text)


def test_parse_shard_reads_k_of_n():
  assert parse_shard('2/3') == Shard(2, 3)


def test_deal_numbers_units_by_first_appearance():
  items = ['a1', 'b1', 'a2', 'c1', 'b2']
  kept, dropped = deal(items, lambda item: item[0], Shard(1, 2))
  assert kept == ['a1', 'a2', 'c1']
  assert dropped == ['b1', 'b2']
