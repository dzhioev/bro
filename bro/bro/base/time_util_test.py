#!/usr/bin/env python
import datetime as dt
import pytz
from base.time_util import (
  Moment,
  Duration,
  UTC,
  utc_now,
  parse_date,
  parse_datetime,
  parse_moment,
  format_time,
  is_naive,
  PAST,
  FUTURE,
)


class TestMoment:
  def test_from_datetime(self):
    d = dt.datetime(2024, 1, 15, 10, 30, 0, tzinfo=pytz.utc)
    m = Moment.from_datetime(d)
    assert isinstance(m, Moment)
    assert m.year == 2024
    assert m.month == 1
    assert m.day == 15
    assert m.hour == 10
    assert m.minute == 30

  def test_now(self):
    m = Moment.now(tz=UTC)
    assert isinstance(m, Moment)
    assert m.tzinfo is not None

  def test_parse(self):
    m = Moment.parse('2024-01-15', '%Y-%m-%d')
    assert isinstance(m, Moment)
    assert m.year == 2024
    assert m.month == 1
    assert m.day == 15

  def test_repr(self):
    m = Moment.from_datetime(dt.datetime(2024, 1, 15, 10, 30, 0, tzinfo=pytz.utc))
    assert repr(m).startswith('Moment(')
    assert '2024-01-15' in repr(m)

  def test_sub_moment_returns_duration(self):
    m1 = Moment.from_datetime(dt.datetime(2024, 1, 15, 10, 0, 0, tzinfo=pytz.utc))
    m2 = Moment.from_datetime(dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=pytz.utc))
    result = m1 - m2
    assert isinstance(result, Duration)
    assert result.total_seconds() == 3600

  def test_sub_duration_returns_moment(self):
    m = Moment.from_datetime(dt.datetime(2024, 1, 15, 10, 0, 0, tzinfo=pytz.utc))
    d = Duration(hours=1)
    result = m - d
    assert isinstance(result, Moment)
    assert result.hour == 9

  def test_add_duration_returns_moment(self):
    m = Moment.from_datetime(dt.datetime(2024, 1, 15, 10, 0, 0, tzinfo=pytz.utc))
    d = Duration(hours=1)
    result = m + d
    assert isinstance(result, Moment)
    assert result.hour == 11

  def test_isinstance_datetime(self):
    m = Moment.now(tz=UTC)
    assert isinstance(m, dt.datetime)


class TestDuration:
  def test_from_timedelta(self):
    td = dt.timedelta(hours=2, minutes=30)
    d = Duration.from_timedelta(td)
    assert isinstance(d, Duration)
    assert d.total_seconds() == 2.5 * 3600

  def test_constructor(self):
    d = Duration(hours=1, minutes=30)
    assert isinstance(d, Duration)
    assert d.total_seconds() == 1.5 * 3600

  def test_repr(self):
    d = Duration(hours=2, minutes=30)
    assert repr(d) == 'Duration(2:30:00)'

  def test_add_duration_returns_duration(self):
    d1 = Duration(hours=1)
    d2 = Duration(minutes=30)
    result = d1 + d2
    assert isinstance(result, Duration)
    assert result.total_seconds() == 1.5 * 3600

  def test_sub_duration_returns_duration(self):
    d1 = Duration(hours=2)
    d2 = Duration(minutes=30)
    result = d1 - d2
    assert isinstance(result, Duration)
    assert result.total_seconds() == 1.5 * 3600

  def test_radd_duration(self):
    d1 = Duration(hours=1)
    d2 = Duration(minutes=30)
    result = sum([d1, d2], Duration())
    assert isinstance(result, Duration)
    assert result.total_seconds() == 1.5 * 3600

  def test_isinstance_timedelta(self):
    d = Duration(hours=1)
    assert isinstance(d, dt.timedelta)


class TestParseFunctions:
  def test_parse_date(self):
    m = parse_date('2024-01-15')
    assert isinstance(m, Moment)
    assert m.year == 2024
    assert m.month == 1
    assert m.day == 15
    assert m.tzinfo == UTC

  def test_parse_datetime(self):
    m = parse_datetime('2024-01-15T10:30:00')
    assert isinstance(m, Moment)
    assert m.year == 2024
    assert m.hour == 10
    assert m.minute == 30

  def test_parse_moment_now(self):
    m = parse_moment('now')
    assert isinstance(m, Moment)

  def test_parse_moment_date(self):
    m = parse_moment('2024-01-15')
    assert isinstance(m, Moment)
    assert m.year == 2024

  def test_parse_moment_datetime(self):
    m = parse_moment('2024-01-15T10:30:00')
    assert isinstance(m, Moment)
    assert m.hour == 10


class TestUtilityFunctions:
  def test_utc_now(self):
    m = utc_now()
    assert isinstance(m, Moment)
    assert m.tzinfo == UTC

  def test_format_time(self):
    m = Moment.from_datetime(dt.datetime(2024, 1, 15, 10, 30, 45, tzinfo=pytz.utc))
    result = format_time(m)
    assert result == '2024-01-15T10:30:45'

  def test_format_time_with_tz(self):
    m = Moment.from_datetime(dt.datetime(2024, 1, 15, 10, 30, 45, tzinfo=pytz.utc))
    result = format_time(m, show_tz_info=True)
    assert 'UTC' in result

  def test_is_naive_false(self):
    m = Moment.from_datetime(dt.datetime(2024, 1, 15, 10, 30, 0, tzinfo=pytz.utc))
    assert not is_naive(m)

  def test_is_naive_true(self):
    m = Moment.from_datetime(dt.datetime(2024, 1, 15, 10, 30, 0))
    assert is_naive(m)


class TestConstants:
  def test_past_is_moment(self):
    assert isinstance(PAST, Moment)

  def test_future_is_moment(self):
    assert isinstance(FUTURE, Moment)

  def test_past_before_future(self):
    assert PAST < FUTURE
