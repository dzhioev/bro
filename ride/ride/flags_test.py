import ride.flags as ride_flags
from bro.base.args import Parser


class TestForwardedFlags:
  def test_extract_forwarded_argv_round_trips_grant_revoke(self):
    parser = Parser(add_help=False)
    ride_flags.add_forwarded_flags(parser)
    args = vars(parser.parse_args(['--grant', 'a', '--grant', '@dev', '--revoke', 'c']))
    assert ride_flags.extract_forwarded_argv(args) == [
      '--grant',
      'a',
      '--grant',
      '@dev',
      '--revoke',
      'c',
    ]

  def test_extract_forwarded_argv_round_trips_into(self):
    parser = Parser(add_help=False)
    ride_flags.add_forwarded_flags(parser)
    args = vars(parser.parse_args(['--into', 'my-branch']))
    assert ride_flags.extract_forwarded_argv(args) == ['--into', 'my-branch']

  def test_extract_forwarded_argv_round_trips_bro_and_raw(self):
    parser = Parser(add_help=False)
    ride_flags.add_forwarded_flags(parser)
    args = vars(parser.parse_args(['--bro', 'dev', '--raw']))
    assert ride_flags.extract_forwarded_argv(args) == ['--bro', 'dev', '--raw']

  def test_extract_forwarded_argv_round_trips_harness(self):
    parser = Parser(add_help=False)
    ride_flags.add_forwarded_flags(parser)
    args = vars(parser.parse_args(['--harness', 'bro']))
    assert ride_flags.extract_forwarded_argv(args) == ['--harness', 'bro']

  def test_llm_flags_default_to_none_for_the_claude_code_spec_to_supply(self):
    parser = Parser(add_help=False)
    ride_flags.add_forwarded_flags(parser)
    args = parser.parse_args([])
    assert (args.llm, args.provider, args.model, args.effort, args.fast) == (
      None,
      None,
      None,
      None,
      False,
    )

  def test_hold_defaults_to_none_for_the_wrapper_to_resolve(self):
    parser = Parser(add_help=False)
    ride_flags.add_forwarded_flags(parser)
    assert parser.parse_args([]).hold is None

  def test_extract_forwarded_argv_round_trips_hold(self):
    parser = Parser(add_help=False)
    ride_flags.add_forwarded_flags(parser)
    args = vars(parser.parse_args(['--hold', 'guided']))
    assert ride_flags.extract_forwarded_argv(args) == ['--hold', 'guided']

  def test_extract_forwarded_argv_elides_an_omitted_hold(self):
    parser = Parser(add_help=False)
    ride_flags.add_forwarded_flags(parser)
    args = vars(parser.parse_args([]))
    assert '--hold' not in ride_flags.extract_forwarded_argv(args)
