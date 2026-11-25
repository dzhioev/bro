import re
import time

import pytest
from ulid import ULID

from bro.base.lulid import _restyle, is_lulid, lulid

# Crockford base32 (lowercase) excludes i, l, o, u
_SHAPE = re.compile(r'[0-9a-hj-km-np-tv-z]{10}-[0-9a-hj-km-np-tv-z]{8}-[0-9a-hj-km-np-tv-z]{8}')


def test_shape():
  assert _SHAPE.fullmatch(lulid()) is not None


def test_unique():
  ids = [lulid() for _ in range(100)]
  assert len(set(ids)) == len(ids)


def test_sorts_by_mint_time():
  earlier = lulid()
  time.sleep(0.002)  # sleep: subject — crosses a millisecond boundary so the prefixes differ
  later = lulid()
  assert earlier < later


def test_restyle_preserves_ulid_order():
  ulids = sorted(str(ULID()) for _ in range(200))
  restyled = [_restyle(ulid_string) for ulid_string in ulids]
  assert restyled == sorted(restyled)


def test_minted_lulid_is_valid():
  assert is_lulid(lulid())


@pytest.mark.parametrize(
  'value',
  [
    '',
    None,
    42,
    '../trail',
    '01kwphn3q5w1fdwep2apw9ag3b',
    '01KWPHN3Q5-W1FDWEP2-APW9AG3B',
    '81kwphn3q5-w1fdwep2-apw9ag3b',
    '01kwphn3q5-w1fdwep2-apw9ag3i',
    '01kwphn3q5-w1fdwep2-apw9ag3b\n',
  ],
)
def test_invalid_lulid(value):
  assert not is_lulid(value)
