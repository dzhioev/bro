"""RingBuffer unit tests — the rest of the module is ports (marker + ABCs),
exercised by the adapters' own suites."""

import pytest

from bro.broker.spawn import RingBuffer


class TestRingBuffer:
  def test_under_cap_keeps_everything(self):
    ring = RingBuffer(100)
    ring.write(b'hello')
    ring.write(b' world')
    assert ring.tail() == b'hello world'

  def test_over_cap_keeps_last_bytes(self):
    ring = RingBuffer(4)
    ring.write(b'abcdefgh')
    assert ring.tail() == b'efgh'

  def test_trims_across_writes(self):
    ring = RingBuffer(4)
    ring.write(b'abc')
    ring.write(b'de')
    assert ring.tail() == b'bcde'

  def test_single_write_larger_than_cap(self):
    ring = RingBuffer(3)
    ring.write(b'abcdefg')
    assert ring.tail() == b'efg'

  def test_exact_cap(self):
    ring = RingBuffer(4)
    ring.write(b'abcd')
    assert ring.tail() == b'abcd'

  def test_negative_cap_rejected(self):
    with pytest.raises(ValueError):
      RingBuffer(-1)
