import cw.flags
from base.args import Parser


class TestForwardedFlags:
  def test_extract_forwarded_argv_round_trips_grant_revoke(self):
    parser = Parser(add_help=False)
    cw.flags.add_forwarded_flags(parser)
    args = vars(parser.parse_args(['--grant-cred', 'a', '--grant-cred', 'b', '--revoke-cred', 'c']))
    assert cw.flags.extract_forwarded_argv(args) == [
      '--grant-cred',
      'a',
      '--grant-cred',
      'b',
      '--revoke-cred',
      'c',
    ]

  def test_extract_forwarded_argv_round_trips_summon_flags(self):
    parser = Parser(add_help=False)
    cw.flags.add_forwarded_flags(parser)
    args = vars(parser.parse_args(['--grant-summon', 'devoops', '--revoke-summon', 'pm']))
    assert cw.flags.extract_forwarded_argv(args) == [
      '--grant-summon',
      'devoops',
      '--revoke-summon',
      'pm',
    ]

  def test_extract_forwarded_argv_round_trips_into(self):
    parser = Parser(add_help=False)
    cw.flags.add_forwarded_flags(parser)
    args = vars(parser.parse_args(['--into', 'my-branch']))
    assert cw.flags.extract_forwarded_argv(args) == ['--into', 'my-branch']

  def test_extract_forwarded_argv_round_trips_bro(self):
    parser = Parser(add_help=False)
    cw.flags.add_forwarded_flags(parser)
    args = vars(parser.parse_args(['--bro', 'ppp-dev']))
    assert cw.flags.extract_forwarded_argv(args) == ['--bro', 'ppp-dev']

  def test_effort_defaults_to_xhigh(self):
    parser = Parser(add_help=False)
    cw.flags.add_forwarded_flags(parser)
    assert parser.parse_args([]).effort == 'xhigh'

  def test_mode_defaults_to_guided(self):
    parser = Parser(add_help=False)
    cw.flags.add_forwarded_flags(parser)
    assert parser.parse_args([]).mode == 'guided'

  def test_extract_forwarded_argv_round_trips_mode(self):
    parser = Parser(add_help=False)
    cw.flags.add_forwarded_flags(parser)
    args = vars(parser.parse_args(['--mode', 'attended']))
    assert cw.flags.extract_forwarded_argv(args) == ['--mode', 'attended']

  def test_extract_forwarded_argv_elides_the_default_mode(self):
    parser = Parser(add_help=False)
    cw.flags.add_forwarded_flags(parser)
    args = vars(parser.parse_args([]))
    assert '--mode' not in cw.flags.extract_forwarded_argv(args)
