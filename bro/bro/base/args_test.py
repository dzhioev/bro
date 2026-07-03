#!/usr/bin/env python
import re
import sys

import pytest

from base.args import REMAINDER, ArgumentTypeError, Parser, list_parser, moment_parser


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
    with pytest.raises(ArgumentTypeError):
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


def _parse_env_table(out: str) -> list[dict[str, str]]:
  lines = [line for line in out.splitlines() if line.strip() != '']
  assert len(lines) > 0, 'expected at least a header row'
  header = re.split(r'\s{2,}', lines[0].strip())
  rows = []
  for line in lines[1:]:
    cells = re.split(r'\s{2,}', line.strip())
    assert len(cells) == len(header), f'row/header width mismatch: {cells!r} vs {header!r}'
    rows.append(dict(zip(header, cells, strict=True)))
  return rows


def _row_for(rows: list[dict[str, str]], env_name: str) -> dict[str, str]:
  matching = [r for r in rows if r['ENV_NAME'] == env_name]
  assert len(matching) == 1, f'expected exactly one row for {env_name}, got {matching!r}'
  return matching[0]


class TestParser:
  def test_parse_method(self):
    parser = Parser()
    parser.add_argument('--foo')
    args = parser.parse(['script.py', '--foo', 'bar'])
    assert args == {'foo': 'bar'}

  def test_global_flags_removed_from_args(self):
    parser = Parser()
    args = parser.parse(['script.py', '--allow-env'])
    assert 'verbose' not in args
    assert 'ic' not in args
    assert 'info' not in args
    assert 'print_env' not in args
    assert 'allow_env' not in args


class TestEnvVarBasics:
  def test_env_ignored_without_allow_env(self, monkeypatch):
    monkeypatch.setenv('FOO', 'from_env')
    parser = Parser()
    parser.add_argument('--foo')
    args = parser.parse_args([])
    assert args.foo is None

  def test_env_applied_with_allow_env(self, monkeypatch):
    monkeypatch.setenv('FOO', 'from_env')
    parser = Parser()
    parser.add_argument('--foo')
    args = parser.parse_args(['--allow-env'])
    assert args.foo == 'from_env'

  def test_cli_overrides_env(self, monkeypatch):
    monkeypatch.setenv('FOO', 'from_env')
    parser = Parser()
    parser.add_argument('--foo')
    args = parser.parse_args(['--allow-env', '--foo', 'from_cli'])
    assert args.foo == 'from_cli'

  def test_env_name_hyphen_to_underscore(self, monkeypatch):
    monkeypatch.setenv('SERVER_URL', 'http://foo')
    parser = Parser()
    parser.add_argument('--server-url')
    args = parser.parse_args(['--allow-env'])
    assert args.server_url == 'http://foo'

  def test_default_used_when_env_unset(self):
    parser = Parser()
    parser.add_argument('--foo', default='default')
    args = parser.parse_args(['--allow-env'])
    assert args.foo == 'default'


class TestEnvTypeCoercion:
  def test_int_type(self, monkeypatch):
    monkeypatch.setenv('PORT', '8080')
    parser = Parser()
    parser.add_argument('--port', type=int)
    args = parser.parse_args(['--allow-env'])
    assert args.port == 8080

  def test_list_parser(self, monkeypatch):
    monkeypatch.setenv('ITEMS', 'a,b,c')
    parser = Parser()
    parser.add_argument('--items', type=list_parser)
    args = parser.parse_args(['--allow-env'])
    assert args.items == ['a', 'b', 'c']

  def test_moment_parser(self, monkeypatch):
    monkeypatch.setenv('WHEN', '2024-01-15')
    parser = Parser()
    parser.add_argument('--when', type=moment_parser)
    args = parser.parse_args(['--allow-env'])
    assert args.when.year == 2024
    assert args.when.month == 1
    assert args.when.day == 15

  def test_bad_type_errors_cleanly(self, monkeypatch):
    monkeypatch.setenv('PORT', 'not-a-number')
    parser = Parser()
    parser.add_argument('--port', type=int)
    with pytest.raises(SystemExit):
      parser.parse_args(['--allow-env'])


class TestEnvBooleans:
  def test_store_true_env_1(self, monkeypatch):
    monkeypatch.setenv('FLAG', '1')
    parser = Parser()
    parser.add_argument('--flag', action='store_true')
    args = parser.parse_args(['--allow-env'])
    assert args.flag is True

  def test_store_true_env_0(self, monkeypatch):
    monkeypatch.setenv('FLAG', '0')
    parser = Parser()
    parser.add_argument('--flag', action='store_true')
    args = parser.parse_args(['--allow-env'])
    assert args.flag is False

  def test_store_true_env_invalid(self, monkeypatch, capsys):
    monkeypatch.setenv('FLAG', 'yes')
    parser = Parser()
    parser.add_argument('--flag', action='store_true')
    with pytest.raises(SystemExit):
      parser.parse_args(['--allow-env'])
    error = capsys.readouterr().err
    assert re.search(r'invalid value for env FLAG', error)

  def test_store_false_env_1_yields_false(self, monkeypatch):
    # store_false default is True; passing the flag sets to False.
    # VAR=1 == "fire the flag" == value becomes False.
    monkeypatch.setenv('QUIET', '1')
    parser = Parser()
    parser.add_argument('--quiet', action='store_false')
    args = parser.parse_args(['--allow-env'])
    assert args.quiet is False

  def test_store_false_env_0_yields_default_true(self, monkeypatch):
    monkeypatch.setenv('QUIET', '0')
    parser = Parser()
    parser.add_argument('--quiet', action='store_false')
    args = parser.parse_args(['--allow-env'])
    assert args.quiet is True

  def test_verbose_env_triggered(self, monkeypatch):
    import logging as _logging

    from base import log

    monkeypatch.setenv('VERBOSE', '1')
    log.set_level(_logging.INFO)
    parser = Parser()
    parser.parse_args(['--allow-env'])
    assert _logging.getLogger('ppp').level == _logging.DEBUG
    log.set_level(_logging.INFO)

  def test_allow_env_itself_not_env_backed(self, monkeypatch):
    monkeypatch.setenv('ALLOW_ENV', '1')
    monkeypatch.setenv('FOO', 'from_env')
    parser = Parser()
    parser.add_argument('--foo')
    args = parser.parse_args([])
    assert args.foo is None


class TestEnvRequiredAndExclusive:
  def test_required_satisfied_by_env(self, monkeypatch):
    monkeypatch.setenv('TOKEN', 'secret')
    parser = Parser()
    parser.add_argument('--token', required=True)
    args = parser.parse_args(['--allow-env'])
    assert args.token == 'secret'

  def test_required_still_errors_without_env_and_without_allow_env(self):
    parser = Parser()
    parser.add_argument('--token', required=True)
    with pytest.raises(SystemExit):
      parser.parse_args([])

  def test_required_errors_without_env_even_with_allow_env(self):
    parser = Parser()
    parser.add_argument('--token', required=True)
    with pytest.raises(SystemExit):
      parser.parse_args(['--allow-env'])

  def test_exclusive_groups_fire_on_env_values(self, monkeypatch):
    monkeypatch.setenv('START', '2024-01-01')
    parser = Parser()
    parser.add_argument('--start')
    parser.add_argument('--yesterday', action='store_true')
    parser.add_exclusive_groups(['start'], ['yesterday'])
    with pytest.raises(SystemExit):
      parser.parse_args(['--allow-env', '--yesterday'])


class TestHelpAnnotation:
  def test_help_annotates_env_name(self):
    parser = Parser()
    parser.add_argument('--server-url', help='server URL')
    parser.parse_args(['--allow-env'])
    help_text = parser.format_help()
    assert '(env: SERVER_URL)' in help_text

  def test_help_annotates_without_user_help(self):
    parser = Parser()
    parser.add_argument('--foo')
    parser.parse_args(['--allow-env'])
    help_text = parser.format_help()
    assert '(env: FOO)' in help_text

  def test_help_omits_env_name_without_allow_env(self):
    parser = Parser()
    parser.add_argument('--server-url', help='server URL')
    help_text = parser.format_help()
    assert '(env: SERVER_URL)' not in help_text

  def test_no_annotation_for_env_false(self):
    parser = Parser()
    parser.add_argument('--token', env=False, help='token')
    help_text = parser.format_help()
    assert '(env: TOKEN)' not in help_text

  def test_no_annotation_for_positional(self):
    parser = Parser()
    parser.add_argument('target', help='target id')
    help_text = parser.format_help()
    assert '(env: TARGET)' not in help_text

  def test_no_annotation_for_allow_env_and_print_env(self):
    parser = Parser()
    help_text = parser.format_help()
    assert '(env: ALLOW_ENV)' not in help_text
    assert '(env: PRINT_ENV)' not in help_text

  def test_no_annotation_for_unsupported_action(self):
    parser = Parser()
    parser.add_argument('--items', nargs='*', help='items')
    help_text = parser.format_help()
    assert '(env: ITEMS)' not in help_text


class TestEnvOptOut:
  def test_env_false_ignores_env(self, monkeypatch):
    monkeypatch.setenv('TOKEN', 'from_env')
    parser = Parser()
    parser.add_argument('--token', env=False)
    args = parser.parse_args(['--allow-env'])
    assert args.token is None

  def test_positional_ignores_env(self, monkeypatch):
    monkeypatch.setenv('TARGET', 'from_env')
    parser = Parser()
    parser.add_argument('target')
    with pytest.raises(SystemExit):
      parser.parse_args(['--allow-env'])


class TestUnsupportedActions:
  def test_nargs_star_registers_ok(self):
    parser = Parser()
    parser.add_argument('--items', nargs='*', default=[])
    args = parser.parse_args([])
    assert args.items == []

  def test_nargs_star_raises_when_env_set(self, monkeypatch):
    monkeypatch.setenv('ITEMS', 'a,b,c')
    parser = Parser()
    parser.add_argument('--items', nargs='*', default=[])
    with pytest.raises(NotImplementedError):
      parser.parse_args(['--allow-env'])

  def test_nargs_star_silent_when_allow_env_off(self, monkeypatch):
    monkeypatch.setenv('ITEMS', 'a,b,c')
    parser = Parser()
    parser.add_argument('--items', nargs='*', default=[])
    args = parser.parse_args([])
    assert args.items == []

  def test_append_raises_when_env_set(self, monkeypatch):
    monkeypatch.setenv('TAG', 'x')
    parser = Parser()
    parser.add_argument('--tag', action='append')
    with pytest.raises(NotImplementedError):
      parser.parse_args(['--allow-env'])

  def test_count_raises_when_env_set(self, monkeypatch):
    monkeypatch.setenv('VV', '2')
    parser = Parser()
    parser.add_argument('--vv', action='count', default=0)
    with pytest.raises(NotImplementedError):
      parser.parse_args(['--allow-env'])


class TestPrintEnv:
  def test_print_env_exits(self):
    parser = Parser()
    parser.add_argument('--foo')
    with pytest.raises(SystemExit) as exception_info:
      parser.parse_args(['--print-env'])
    assert exception_info.value.code == 0

  def test_print_env_includes_verbose_and_ic(self, capsys):
    parser = Parser()
    with pytest.raises(SystemExit):
      parser.parse_args(['--print-env'])
    env_names = {r['ENV_NAME'] for r in _parse_env_table(capsys.readouterr().out)}
    assert 'VERBOSE' in env_names
    assert 'IC' in env_names
    assert 'ALLOW_ENV' not in env_names
    assert 'PRINT_ENV' not in env_names

  def test_print_env_src_default(self, capsys):
    parser = Parser()
    parser.add_argument('--foo', default='x')
    with pytest.raises(SystemExit):
      parser.parse_args(['--print-env'])
    row = _row_for(_parse_env_table(capsys.readouterr().out), 'FOO')
    assert row == {'SRC': 'D', 'ENV_NAME': 'FOO', 'CURRENT VALUE': 'x', 'ENV VALUE': '(not set)'}

  def test_print_env_src_argument(self, capsys):
    parser = Parser()
    parser.add_argument('--foo')
    with pytest.raises(SystemExit):
      parser.parse_args(['--foo', 'bar', '--print-env'])
    row = _row_for(_parse_env_table(capsys.readouterr().out), 'FOO')
    assert row == {'SRC': 'A', 'ENV_NAME': 'FOO', 'CURRENT VALUE': 'bar', 'ENV VALUE': '(not set)'}

  def test_print_env_src_env(self, capsys, monkeypatch):
    monkeypatch.setenv('FOO', 'from_env')
    parser = Parser()
    parser.add_argument('--foo')
    with pytest.raises(SystemExit):
      parser.parse_args(['--allow-env', '--print-env'])
    row = _row_for(_parse_env_table(capsys.readouterr().out), 'FOO')
    assert row == {
      'SRC': 'E',
      'ENV_NAME': 'FOO',
      'CURRENT VALUE': 'from_env',
      'ENV VALUE': 'from_env',
    }

  def test_print_env_shows_lurking_env_when_allow_env_off(self, capsys, monkeypatch):
    monkeypatch.setenv('FOO', 'lurking')
    parser = Parser()
    parser.add_argument('--foo', default='default')
    with pytest.raises(SystemExit):
      parser.parse_args(['--print-env'])
    row = _row_for(_parse_env_table(capsys.readouterr().out), 'FOO')
    assert row == {
      'SRC': 'D',
      'ENV_NAME': 'FOO',
      'CURRENT VALUE': 'default',
      'ENV VALUE': 'lurking',
    }

  def test_print_env_masks_secret(self, capsys, monkeypatch):
    monkeypatch.setenv('TOKEN', 'super-secret')
    parser = Parser()
    parser.add_argument('--token', secret=True)
    with pytest.raises(SystemExit):
      parser.parse_args(['--allow-env', '--print-env'])
    out = capsys.readouterr().out
    assert 'super-secret' not in out
    row = _row_for(_parse_env_table(out), 'TOKEN')
    assert row == {'SRC': 'E', 'ENV_NAME': 'TOKEN', 'CURRENT VALUE': '***', 'ENV VALUE': '***'}

  def test_print_env_excludes_env_false(self, capsys):
    parser = Parser()
    parser.add_argument('--foo')
    parser.add_argument('--bar', env=False)
    with pytest.raises(SystemExit):
      parser.parse_args(['--print-env'])
    rows = _parse_env_table(capsys.readouterr().out)
    env_names = {r['ENV_NAME'] for r in rows}
    assert 'FOO' in env_names
    assert 'BAR' not in env_names

  def test_print_env_excludes_unsupported_action(self, capsys):
    parser = Parser()
    parser.add_argument('--foo')
    parser.add_argument('--items', nargs='*')
    with pytest.raises(SystemExit):
      parser.parse_args(['--print-env'])
    rows = _parse_env_table(capsys.readouterr().out)
    env_names = {r['ENV_NAME'] for r in rows}
    assert 'FOO' in env_names
    assert 'ITEMS' not in env_names

  def test_print_env_bool_formatting(self, capsys):
    parser = Parser()
    parser.add_argument('--flag', action='store_true')
    with pytest.raises(SystemExit):
      parser.parse_args(['--flag', '--print-env'])
    row = _row_for(_parse_env_table(capsys.readouterr().out), 'FLAG')
    assert row == {'SRC': 'A', 'ENV_NAME': 'FLAG', 'CURRENT VALUE': '1', 'ENV VALUE': '(not set)'}

  def test_print_env_list_formatting(self, capsys):
    parser = Parser()
    parser.add_argument('--items', type=list_parser, default=['x', 'y'])
    with pytest.raises(SystemExit):
      parser.parse_args(['--print-env'])
    row = _row_for(_parse_env_table(capsys.readouterr().out), 'ITEMS')
    assert row == {
      'SRC': 'D',
      'ENV_NAME': 'ITEMS',
      'CURRENT VALUE': 'x,y',
      'ENV VALUE': '(not set)',
    }


class TestReconstruct:
  def test_store_true_flags(self):
    parser = Parser()
    parser.add_argument('-c', '--container', action='store_true')
    parser.add_argument('--drop', action='store_true')
    args = parser.parse(['cmd', '-c'])
    assert parser.reconstruct(args) == [parser.prog, '-c']

  def test_value_arg_included_when_set(self):
    parser = Parser()
    parser.add_argument('--name', default=None)
    args = parser.parse(['cmd', '--name', 'foo'])
    assert parser.reconstruct(args) == [parser.prog, '--name', 'foo']

  def test_value_arg_omitted_when_default(self):
    parser = Parser()
    parser.add_argument('--name', default='team')
    args = parser.parse(['cmd'])
    assert parser.reconstruct(args) == [parser.prog]

  def test_positional_and_remainder(self):
    parser = Parser()
    parser.add_argument('target')
    parser.add_argument('extra', nargs=REMAINDER)
    args = parser.parse(['cmd', 'mytarget', '--foo', 'bar'])
    assert parser.reconstruct(args) == [parser.prog, 'mytarget', '--foo', 'bar']

  def test_prog_override_string(self):
    parser = Parser()
    parser.add_argument('--flag', action='store_true')
    args = parser.parse(['cmd', '--flag'])
    assert parser.reconstruct(args, prog='mycli') == ['mycli', '--flag']

  def test_prog_override_list(self):
    parser = Parser()
    parser.add_argument('--flag', action='store_true')
    args = parser.parse(['cmd', '--flag'])
    assert parser.reconstruct(args, prog=['cw', 'ss']) == ['cw', 'ss', '--flag']

  def test_exclude(self):
    parser = Parser()
    parser.add_argument('-c', '--container', action='store_true')
    parser.add_argument('--auto', action='store_true')
    parser.add_argument('-p', '--prompt', default=None)
    args = parser.parse(['cmd', '-c', '--auto', '-p', 'hello'])
    result = parser.reconstruct(args, exclude=('prompt',))
    assert result == [parser.prog, '-c', '--auto']

  def test_global_flags_excluded(self):
    parser = Parser()
    parser.add_argument('--flag', action='store_true')
    args = parser.parse(['cmd', '--flag'])
    # global flags like --verbose, --ic are not in the output
    result = parser.reconstruct(args)
    assert '--verbose' not in result
    assert '--ic' not in result
    assert '--allow-env' not in result
    assert '--flag' in result

  def test_namespace_input(self):
    parser = Parser()
    parser.add_argument('--flag', action='store_true')
    namespace = parser.parse_args(['--flag'])
    result = parser.reconstruct(namespace)
    assert result == [parser.prog, '--flag']

  def test_empty_remainder(self):
    parser = Parser()
    parser.add_argument('name')
    parser.add_argument('rest', nargs=REMAINDER)
    args = parser.parse(['cmd', 'foo'])
    assert parser.reconstruct(args) == [parser.prog, 'foo']

  def test_append_action_repeats_flag(self):
    parser = Parser()
    parser.add_argument('--grant', action='append', default=None)
    args = parser.parse(['cmd', '--grant', 'a', '--grant', 'b'])
    assert parser.reconstruct(args) == [parser.prog, '--grant', 'a', '--grant', 'b']

  def test_append_action_omitted_when_none(self):
    parser = Parser()
    parser.add_argument('--grant', action='append', default=None)
    args = parser.parse(['cmd'])
    assert parser.reconstruct(args) == [parser.prog]

  def test_subparser_reconstruct(self):
    parser = Parser()
    subparsers = parser.add_subparsers(dest='cmd')
    subparser = subparsers.add_parser('ss')
    subparser.add_argument('-c', '--container', action='store_true')
    subparser.add_argument('--mcp', action='store_true')
    subparser.add_argument('name')
    subparser.add_argument('extra', nargs=REMAINDER)
    args = parser.parse(['cmd', 'ss', '-c', '--mcp', 'myname', '--foo'])
    result = subparser.reconstruct(args, prog=['cw', 'ss'])
    assert result == ['cw', 'ss', '-c', '--mcp', 'myname', '--foo']


class TestParseArgvBoundary:
  def test_parse_args_ignores_sys_argv(self, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['prog', '--foo', 'leaked'])
    parser = Parser()
    parser.add_argument('--foo')
    args = parser.parse_args([])
    assert args.foo is None

  def test_parse_strips_prog_name(self):
    parser = Parser()
    parser.add_argument('--foo')
    assert parser.parse(['prog', '--foo', 'bar']) == {'foo': 'bar'}


class TestDispatch:
  def _build(self) -> Parser:
    parser = Parser()
    subparsers = parser.add_subparsers(dest='cmd')
    create = subparsers.add_parser('create')
    create.add_argument('--title')
    create.set_handler(lambda title=None: ('create', title))
    subparsers.add_parser('list').set_handler(lambda: 'listed')
    return parser

  def test_routes_with_kwargs(self):
    assert self._build().dispatch(['prog', 'create', '--title', 'hi']) == ('create', 'hi')

  def test_routes_no_arg_subcommand(self):
    assert self._build().dispatch(['prog', 'list']) == 'listed'

  def test_no_subcommand_prints_help_returns_1(self, capsys):
    assert self._build().dispatch(['prog']) == 1
    assert 'usage' in capsys.readouterr().err.lower()

  def test_requires_subparsers(self):
    parser = Parser()
    with pytest.raises(RuntimeError):
      parser.dispatch(['prog'])

  def test_set_handler_returns_parser(self):
    parser = Parser()
    subparsers = parser.add_subparsers(dest='cmd')
    p = subparsers.add_parser('x')
    assert p.set_handler(lambda: None) is p


class TestStdlibOnlyImport:
  def test_imports_and_parses_without_icecream(self):
    # base.args must import in a stdlib-only environment (no venv); icecream is
    # optional. simulate its absence in a fresh subprocess. cwd is the repo root
    # (run_tests invokes pytest there), so `import base.args` resolves.
    import subprocess

    code = (
      "import sys; sys.modules['icecream'] = None; "
      'import base.args; '
      "namespace = base.args.Parser().parse(['prog']); "
      "assert 'ic' not in namespace and '--ic' not in base.args.Parser().format_help(); "
      "print('ok')"
    )
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'
