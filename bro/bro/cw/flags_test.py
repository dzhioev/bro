import bro.cw.flags as cw_flags
from bro.base.args import Parser


class TestForwardedFlags:
  def test_extract_forwarded_argv_round_trips_grant_revoke(self):
    parser = Parser(add_help=False)
    cw_flags.add_forwarded_flags(parser)
    args = vars(parser.parse_args(['--grant', 'a', '--grant', '@devoops', '--revoke', 'c']))
    assert cw_flags.extract_forwarded_argv(args) == [
      '--grant',
      'a',
      '--grant',
      '@devoops',
      '--revoke',
      'c',
    ]

  def test_extract_forwarded_argv_round_trips_into(self):
    parser = Parser(add_help=False)
    cw_flags.add_forwarded_flags(parser)
    args = vars(parser.parse_args(['--into', 'my-branch']))
    assert cw_flags.extract_forwarded_argv(args) == ['--into', 'my-branch']

  def test_extract_forwarded_argv_round_trips_bro_and_raw(self):
    parser = Parser(add_help=False)
    cw_flags.add_forwarded_flags(parser)
    args = vars(parser.parse_args(['--bro', 'ppp-dev', '--raw']))
    assert cw_flags.extract_forwarded_argv(args) == ['--bro', 'ppp-dev', '--raw']

  def test_effort_defaults_to_xhigh(self):
    parser = Parser(add_help=False)
    cw_flags.add_forwarded_flags(parser)
    assert parser.parse_args([]).effort == 'xhigh'

  def test_hold_defaults_to_none_for_the_wrapper_to_resolve(self):
    parser = Parser(add_help=False)
    cw_flags.add_forwarded_flags(parser)
    assert parser.parse_args([]).hold is None

  def test_extract_forwarded_argv_round_trips_hold(self):
    parser = Parser(add_help=False)
    cw_flags.add_forwarded_flags(parser)
    args = vars(parser.parse_args(['--hold', 'guided']))
    assert cw_flags.extract_forwarded_argv(args) == ['--hold', 'guided']

  def test_extract_forwarded_argv_elides_an_omitted_hold(self):
    parser = Parser(add_help=False)
    cw_flags.add_forwarded_flags(parser)
    args = vars(parser.parse_args([]))
    assert '--hold' not in cw_flags.extract_forwarded_argv(args)
