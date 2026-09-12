#!/usr/bin/env python
import datetime as dt
from types import EllipsisType
from typing import Optional, Self, SupportsIndex, overload
from zoneinfo import ZoneInfo

import dateutil.parser

from bro.base import log
from bro.base.args import Parser

UTC = dt.UTC
timezone = ZoneInfo

LOCAL_TZ_NAME = 'Europe/Nicosia'


def local_tz() -> ZoneInfo:
  return ZoneInfo(LOCAL_TZ_NAME)


DATE_FORMAT = '%Y-%m-%d'
DATETIME_FORMAT = '%Y-%m-%dT%H:%M:%S'

date = dt.date
datetime = dt.datetime
timedelta = dt.timedelta


class Moment(dt.datetime):
  @overload
  def __new__(
    cls,
    year: SupportsIndex,
    month: SupportsIndex,
    day: SupportsIndex,
    hour: SupportsIndex = 0,
    minute: SupportsIndex = 0,
    second: SupportsIndex = 0,
    microsecond: SupportsIndex = 0,
    tzinfo: dt.tzinfo | None = None,
    *,
    fold: int = 0,
  ) -> Self: ...

  @overload
  def __new__(cls, payload: bytes, timezone: dt.tzinfo | None = None, /) -> Self: ...

  def __new__(cls, *arguments: object, **keyword_arguments: object) -> Self:
    positional_arguments = list(arguments)
    # datetime's pickle protocol reconstructs subclasses from a binary payload and optional timezone.
    if positional_arguments and isinstance(positional_arguments[0], bytes):
      if len(positional_arguments) == 1:
        positional_arguments.append(UTC)
      elif positional_arguments[1] is None:
        positional_arguments[1] = UTC
    elif len(positional_arguments) > 7:
      if positional_arguments[7] is None:
        positional_arguments[7] = UTC
    elif keyword_arguments.get('tzinfo') is None:
      keyword_arguments['tzinfo'] = UTC

    moment = super().__new__(cls, *positional_arguments, **keyword_arguments)  # type: ignore[arg-type]
    if moment.utcoffset() is None:
      raise ValueError('Moment requires a timezone with a UTC offset')
    return moment

  def __repr__(self) -> str:
    return f'Moment({self.isoformat()})'

  def replace(
    self,
    year: SupportsIndex | EllipsisType = ...,
    month: SupportsIndex | EllipsisType = ...,
    day: SupportsIndex | EllipsisType = ...,
    hour: SupportsIndex | EllipsisType = ...,
    minute: SupportsIndex | EllipsisType = ...,
    second: SupportsIndex | EllipsisType = ...,
    microsecond: SupportsIndex | EllipsisType = ...,
    tzinfo: dt.tzinfo | None | EllipsisType = ...,
    *,
    fold: int | EllipsisType = ...,
  ) -> Self:
    # datetime.replace may construct a subclass without calling its __new__ method.
    changes = {
      'year': year,
      'month': month,
      'day': day,
      'hour': hour,
      'minute': minute,
      'second': second,
      'microsecond': microsecond,
      'tzinfo': tzinfo,
      'fold': fold,
    }
    replaced = super().replace(
      **{name: value for name, value in changes.items() if value is not Ellipsis}
    )  # type: ignore[arg-type]
    return type(self).from_datetime(replaced)

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
    return cls(
      d.year,
      d.month,
      d.day,
      d.hour,
      d.minute,
      d.second,
      d.microsecond,
      d.tzinfo,
      fold=d.fold,
    )

  @classmethod
  def fromisoformat(cls, s: str) -> Self:
    return cls.from_datetime(dt.datetime.fromisoformat(s))

  @classmethod
  def now(cls, tz: Optional[dt.tzinfo] = None) -> Self:
    timezone = tz if tz is not None else UTC
    return cls.from_datetime(dt.datetime.now(tz=timezone))

  @classmethod
  def today(cls) -> Self:
    return cls.now()

  @classmethod
  def fromtimestamp(cls, timestamp: float, tz: Optional[dt.tzinfo] = None) -> Self:
    timezone = tz if tz is not None else UTC
    return cls.from_datetime(dt.datetime.fromtimestamp(timestamp, tz=timezone))

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


def utc_now() -> Moment:
  return Moment.now(tz=UTC)


def parse_moment(s: str) -> Moment:
  if s == 'now':
    return utc_now()
  try:
    return Moment.fromisoformat(s)
  except ValueError as error:
    log.debug(f'"{s}": {error}')
    raise ValueError(f'failed to parse moment: "{s}"') from error


PAST = parse_date('1988-01-01')
FUTURE = parse_date('2188-01-01')


def format_now(show_tz_info: bool, zone: Optional[str]) -> str:
  now = utc_now()

  if zone is not None:
    now = now.astimezone(timezone(zone))

  return format_time(now, show_tz_info=show_tz_info)


def print_current_time(show_tz_info: bool, zone: Optional[str]) -> None:
  current_time = format_now(show_tz_info, zone)
  print(current_time)


def main(argv: list[str]) -> Optional[int]:
  parser = Parser(description='print current time')
  parser.add_argument('-z', dest='show_tz_info', help='show tz info', action='store_true')
  parser.add_argument('--zone', help='convert time to the timezone')
  print_current_time(**parser.parse(argv))
  return 0
