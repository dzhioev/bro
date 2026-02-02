#!/usr/bin/env python
import pytest
from base.args import Parser, moment_parser, list_parser


class TestExclusiveGroups:
  def test_exclusive_groups_allows_first_group(self):
    parser = Parser()
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--yesterday', action='store_true')
    parser.add_exclusive_groups(['start', 'end'], ['yesterday'])

    args = parser.parse_args(['--start', '2024-01-01', '--end', '2024-01-02'])
    assert args.start == '2024-01-01'
    assert args.end == '2024-01-02'
    assert args.yesterday is False

  def test_exclusive_groups_allows_second_group(self):
    parser = Parser()
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--yesterday', action='store_true')
    parser.add_exclusive_groups(['start', 'end'], ['yesterday'])

    args = parser.parse_args(['--yesterday'])
    assert args.start is None
    assert args.end is None
    assert args.yesterday is True

  def test_exclusive_groups_allows_partial_first_group(self):
    parser = Parser()
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--yesterday', action='store_true')
    parser.add_exclusive_groups(['start', 'end'], ['yesterday'])

    args = parser.parse_args(['--start', '2024-01-01'])
    assert args.start == '2024-01-01'
    assert args.end is None

  def test_exclusive_groups_rejects_mixed_usage(self):
    parser = Parser()
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--yesterday', action='store_true')
    parser.add_exclusive_groups(['start', 'end'], ['yesterday'])

    with pytest.raises(SystemExit):
      parser.parse_args(['--start', '2024-01-01', '--yesterday'])

  def test_exclusive_groups_rejects_all_groups(self):
    parser = Parser()
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--yesterday', action='store_true')
    parser.add_exclusive_groups(['start', 'end'], ['yesterday'])

    with pytest.raises(SystemExit):
      parser.parse_args(['--start', '2024-01-01', '--end', '2024-01-02', '--yesterday'])

  def test_exclusive_groups_in_help(self):
    parser = Parser()
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--yesterday', action='store_true')
    parser.add_exclusive_groups(['start', 'end'], ['yesterday'])

    help_text = parser.format_help()
    assert 'Constraints:' in help_text
    assert '{--start, --end}' in help_text
    assert '--yesterday' in help_text
    assert 'mutually exclusive' in help_text

  def test_exclusive_groups_single_item_group_no_braces(self):
    parser = Parser()
    parser.add_argument('--foo')
    parser.add_argument('--bar')
    parser.add_exclusive_groups(['foo'], ['bar'])

    help_text = parser.format_help()
    assert '--foo | --bar' in help_text
    assert '{--foo}' not in help_text

  def test_exclusive_groups_hyphenated_args(self):
    parser = Parser()
    parser.add_argument('--group-by')
    parser.add_argument('--quick', action='store_true')
    parser.add_exclusive_groups(['group_by'], ['quick'])

    help_text = parser.format_help()
    assert '--group-by' in help_text

  def test_no_exclusive_groups_no_constraints_in_help(self):
    parser = Parser()
    parser.add_argument('--foo')

    help_text = parser.format_help()
    assert 'Constraints:' not in help_text


class TestMomentParser:
  def test_parses_date(self):
    m = moment_parser('2024-01-15')
    assert m.year == 2024
    assert m.month == 1
    assert m.day == 15

  def test_parses_datetime(self):
    m = moment_parser('2024-01-15T10:30:00')
    assert m.hour == 10
    assert m.minute == 30

  def test_parses_now(self):
    m = moment_parser('now')
    assert m is not None

  def test_invalid_raises_argument_type_error(self):
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
      moment_parser('invalid-date')


class TestListParser:
  def test_parses_single_item(self):
    result = list_parser('foo')
    assert result == ['foo']

  def test_parses_multiple_items(self):
    result = list_parser('foo,bar,baz')
    assert result == ['foo', 'bar', 'baz']

  def test_strips_whitespace(self):
    result = list_parser('foo , bar , baz')
    assert result == ['foo', 'bar', 'baz']


class TestParser:
  def test_parse_method(self):
    parser = Parser()
    parser.add_argument('--foo')
    args = parser.parse(['script.py', '--foo', 'bar'])
    assert args == {'foo': 'bar'}

  def test_verbose_and_ic_removed_from_args(self):
    parser = Parser()
    args = parser.parse(['script.py'])
    assert 'verbose' not in args
    assert 'ic' not in args
    assert 'info' not in args
