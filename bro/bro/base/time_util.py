#!/usr/bin/env python
from typing import Self, overload
from zoneinfo import ZoneInfo
import sys
from base import log
import datetime as dt

import dateutil.parser

UTC = dt.timezone.utc
timezone = ZoneInfo

DATE_FORMAT = '%Y-%m-%d'
DATETIME_FORMAT = '%Y-%m-%dT%H:%M:%S'

date = dt.date
datetime = dt.datetime
timedelta = dt.timedelta


class Moment(dt.datetime):
  def __repr__(self) -> str:
    return f'Moment({self.isoformat()})'

  @overload  # type: ignore[override]
  def __sub__(self, other: 'Moment') -> 'Duration': ...
  @overload  # type: ignore[override]
  def __sub__(self, other: 'Duration') -> 'Moment': ...
  def __sub__(self, other: 'Moment | Duration') -> 'Moment | Duration':  # type: ignore[override]
    if isinstance(other, dt.datetime):
      return Duration.from_timedelta(dt.datetime.__sub__(self, other))
    return Moment.from_datetime(dt.datetime.__sub__(self, other))

  def __add__(self, other: 'Duration') -> 'Moment':  # type: ignore[override]
    return Moment.from_datetime(super().__add__(other))

  @classmethod
  def from_datetime(cls, d: dt.datetime) -> Self:
    return cls(d.year, d.month, d.day, d.hour, d.minute, d.second, d.microsecond, d.tzinfo)

  @classmethod
  def fromisoformat(cls, s: str) -> Self:
    return cls.from_datetime(dt.datetime.fromisoformat(s))

  @classmethod
  def now(cls, tz: dt.tzinfo | None = None) -> Self:
    return cls.from_datetime(dt.datetime.now(tz=tz))

  @classmethod
  def parse(cls, s: str, format: str) -> Self:
    return cls.from_datetime(dt.datetime.strptime(s, format))


class Duration(dt.timedelta):
  def __repr__(self) -> str:
    return f'Duration({super().__str__()})'

  def __add__(self, other: 'Duration') -> 'Duration':  # type: ignore[override]
    return Duration.from_timedelta(super().__add__(other))

  def __radd__(self, other: 'Duration') -> 'Duration':  # type: ignore[override]
    return Duration.from_timedelta(super().__radd__(other))

  def __sub__(self, other: 'Duration') -> 'Duration':  # type: ignore[override]
    return Duration.from_timedelta(super().__sub__(other))

  @classmethod
  def from_timedelta(cls, td: dt.timedelta) -> Self:
    return cls(seconds=td.total_seconds())


def parse_date(s: str) -> Moment:
  return Moment.from_datetime(dt.datetime.strptime(s, DATE_FORMAT).replace(tzinfo=UTC))


def parse_datetime(s: str) -> Moment:
  return Moment.from_datetime(datetime.strptime(s, DATETIME_FORMAT).replace(tzinfo=UTC))


def parse_iso_timestamp(timestamp: str) -> Moment:
  return Moment.from_datetime(dateutil.parser.isoparse(timestamp).astimezone(UTC))


def format_time(d: Moment, show_tz_info: bool = False) -> str:
  template = '%Y-%m-%dT%H:%M:%S'
  if show_tz_info:
    tzname = d.tzname()
    template += f' {tzname}%z'
  return d.strftime(template)


def is_naive(moment: Moment) -> bool:
  return moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None


def utc_now() -> Moment:
  return Moment.now(tz=UTC)


def parse_moment(s: str) -> Moment:
  if s == 'now':
    return utc_now()
  try:
    return parse_datetime(s)
  except Exception as e:
    log.debug(f'"{s}": {e})')

  try:
    return parse_date(s)
  except Exception as e:
    log.debug(f'"{s}": {e})')

  raise ValueError(f'failed to parse moment: "{s}"')


PAST = parse_date('1988-01-01')
FUTURE = parse_date('2188-01-01')


def format_now(show_tz_info: bool, zone: str | None) -> str:
  now = utc_now()

  if zone is not None:
    now = now.astimezone(timezone(zone))

  return format_time(now, show_tz_info=show_tz_info)


def print_current_time(show_tz_info: bool, zone: str | None) -> None:
  current_time = format_now(show_tz_info, zone)
  print(current_time)


def main(argv: list[str] | None = None) -> int:
  import base.args

  parser = base.args.Parser(description='print current time')
  parser.add_argument('-z', dest='show_tz_info', help='show tz info', action='store_true')
  parser.add_argument('--zone', help='convert time to the timezone')
  print_current_time(**parser.parse(argv))
  return 0


if __name__ == '__main__':
  sys.exit(main(sys.argv))
