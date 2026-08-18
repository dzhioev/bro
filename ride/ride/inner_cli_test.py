from unittest.mock import patch

import pytest

import ride.cli as ride_cli


def test_along_in_place_dispatches_to_the_harness_runner():
  with patch('ride.claude.harness.CLAUDE.run_in_place', return_value=0) as run:
    assert (
      ride_cli.main(
        [
          'ride',
          'along',
          '--in-place',
          '--workspace',
          'w',
          '--harness',
          'claude',
          '--resume',
          'dev',
        ]
      )
      == 0
    )
  spec = run.call_args.args[0]
  assert spec.name == 'w'
  assert spec.resume
  assert not spec.solo


def test_in_place_rejects_outer_machinery():
  with pytest.raises(SystemExit):
    ride_cli.main(
      [
        'ride',
        'along',
        '--in-place',
        '--workspace',
        'w',
        '--host',
        'dev',
      ]
    )


def _outer_spec(argv: list[str]):
  with patch('ride.cli.start_session', return_value=0) as start:
    assert ride_cli.main(argv) == 0
  return start.call_args.args[0]


def _inner_spec(argv: list[str]):
  with patch('ride.claude.harness.CLAUDE.run_in_place', return_value=0) as run:
    assert ride_cli.main(argv) == 0
  return run.call_args.args[0]


class TestHoldRoundTrip:
  """the inner argv cannot carry --host, so the outer-resolved hold must reach the
  inner parse explicitly rather than through re-derivation."""

  @pytest.mark.parametrize(
    ('solo', 'host', 'resolved'),
    [
      (False, False, 'attended'),
      (False, True, 'guided'),
      (True, False, 'unattended'),
      (True, True, 'unattended'),
    ],
  )
  def test_the_outer_resolved_hold_survives_the_inner_parse(self, solo, host, resolved):
    verb = 'solo' if solo else 'along'
    argv = ['ride', verb, '--workspace', 'w', '--harness', 'claude']
    if host:
      argv.append('--host')
    argv.append('dev')
    if solo:
      argv.append('go')
    outer = _outer_spec(argv)
    assert outer.hold == resolved
    assert _inner_spec(outer.inner_command()).hold == resolved

  def test_an_explicit_guided_hold_survives_the_inner_parse(self):
    outer = _outer_spec(
      ['ride', 'along', '--workspace', 'w', '--harness', 'claude', '--hold', 'guided', 'dev']
    )
    assert _inner_spec(outer.inner_command()).hold == 'guided'
