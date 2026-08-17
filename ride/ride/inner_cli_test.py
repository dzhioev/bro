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
