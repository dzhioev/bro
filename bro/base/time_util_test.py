#!/usr/bin/env python
import datetime as dt
import pickle

import pytest

from bro.base.time_util import (
  FUTURE,
  PAST,
  UTC,
  Duration,
  Moment,
  format_time,
  parse_date,
  parse_datetime,
  parse_moment,
  timezone,
  utc_now,
)


class TestMoment:
  def test_constructor_assigns_utc_by_default(self):
    moment = Moment(2024, 1, 15, 10, 30)
    assert moment.tzinfo == UTC

  def test_constructor_rejects_timezone_without_offset(self):
    class MissingOffsetTimezone(dt.tzinfo):
      def utcoffset(self, moment: dt.datetime | None) -> None:
        return None

    with pytest.raises(ValueError, match='requires a timezone with a UTC offset'):
      Moment(2024, 1, 15, tzinfo=MissingOffsetTimezone())

  def test_from_datetime(self):
    d = dt.datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
    m = Moment.from_datetime(d)
    assert isinstance(m, Moment)
    assert m.year == 2024
    assert m.month == 1
    assert m.day == 15
    assert m.hour == 10
    assert m.minute == 30

  def test_from_datetime_assigns_utc_to_naive_value(self):
    moment = Moment.from_datetime(dt.datetime(2024, 1, 15, 10, 30))
    assert moment.tzinfo == UTC

  def test_fromisoformat_assigns_utc_to_naive_value(self):
    moment = Moment.fromisoformat('2024-01-15T10:30:00')
    assert moment.tzinfo == UTC

  def test_now(self):
    moment = Moment.now()
    assert isinstance(moment, Moment)
    assert moment.tzinfo == UTC

  def test_fromtimestamp_defaults_to_utc(self):
    moment = Moment.fromtimestamp(0)
    assert moment == Moment(1970, 1, 1)

  def test_fromtimestamp_preserves_fold(self):
    timestamp = dt.datetime(2024, 11, 3, 6, 30, tzinfo=UTC).timestamp()
    moment = Moment.fromtimestamp(timestamp, timezone('America/New_York'))
    assert moment.fold == 1
    assert moment.timestamp() == timestamp

  def test_parse(self):
    m = Moment.parse('2024-01-15', '%Y-%m-%d')
    assert isinstance(m, Moment)
    assert m.year == 2024
    assert m.month == 1
    assert m.day == 15
    assert m.tzinfo == UTC

  def test_replace_cannot_remove_timezone(self):
    moment = Moment(2024, 1, 15, tzinfo=UTC).replace(tzinfo=None)
    assert moment.tzinfo == UTC

  def test_pickle_round_trip_preserves_timezone(self):
    restored = pickle.loads(pickle.dumps(Moment(2024, 1, 15, tzinfo=UTC)))
    assert restored == Moment(2024, 1, 15, tzinfo=UTC)
    assert restored.tzinfo == UTC

  def test_repr(self):
    m = Moment.from_datetime(dt.datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC))
    assert repr(m).startswith('Moment(')
    assert '2024-01-15' in repr(m)

  def test_sub_moment_returns_duration(self):
    m1 = Moment.from_datetime(dt.datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC))
    m2 = Moment.from_datetime(dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=UTC))
    result = m1 - m2
    assert isinstance(result, Duration)
    assert result.total_seconds() == 3600

  def test_sub_duration_returns_moment(self):
    m = Moment.from_datetime(dt.datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC))
    d = Duration(hours=1)
    result = m - d
    assert isinstance(result, Moment)
    assert result.hour == 9

  def test_add_duration_returns_moment(self):
    m = Moment.from_datetime(dt.datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC))
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

  def test_parse_moment_date_as_utc(self):
    m = parse_moment('2024-01-15')
    assert m == Moment(2024, 1, 15, tzinfo=UTC)

  def test_parse_moment_naive_datetime_as_utc(self):
    m = parse_moment('2024-01-15T10:30:00')
    assert m == Moment(2024, 1, 15, 10, 30, tzinfo=UTC)

  def test_parse_moment_datetime_preserves_offset(self):
    m = parse_moment('2024-01-15T10:30:00+03:00')
    assert m.isoformat() == '2024-01-15T10:30:00+03:00'


class TestUtilityFunctions:
  def test_utc_now(self):
    m = utc_now()
    assert isinstance(m, Moment)
    assert m.tzinfo == UTC

  def test_format_time(self):
    m = Moment.from_datetime(dt.datetime(2024, 1, 15, 10, 30, 45, tzinfo=UTC))
    result = format_time(m)
    assert result == '2024-01-15T10:30:45'

  def test_format_time_with_tz(self):
    m = Moment.from_datetime(dt.datetime(2024, 1, 15, 10, 30, 45, tzinfo=UTC))
    result = format_time(m, show_tz_info=True)
    assert 'UTC' in result


class TestConstants:
  def test_past_is_moment(self):
    assert isinstance(PAST, Moment)

  def test_future_is_moment(self):
    assert isinstance(FUTURE, Moment)

  def test_past_before_future(self):
    assert PAST < FUTURE
