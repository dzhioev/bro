import re
import time

from ulid import ULID

from base.lulid import _restyle, lulid

# Crockford base32 (lowercase) excludes i, l, o, u
_SHAPE = re.compile(r'[0-9a-hj-km-np-tv-z]{10}-[0-9a-hj-km-np-tv-z]{8}-[0-9a-hj-km-np-tv-z]{8}')


def test_shape():
  assert _SHAPE.fullmatch(lulid()) is not None


def test_unique():
  ids = [lulid() for _ in range(100)]
  assert len(set(ids)) == len(ids)


def test_sorts_by_mint_time():
  earlier = lulid()
  time.sleep(0.002)  # cross a millisecond boundary so the timestamp prefixes differ
  later = lulid()
  assert earlier < later


def test_restyle_preserves_ulid_order():
  ulids = sorted(str(ULID()) for _ in range(200))
  restyled = [_restyle(ulid_string) for ulid_string in ulids]
  assert restyled == sorted(restyled)
