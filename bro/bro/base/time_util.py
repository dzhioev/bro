#!/usr/bin/env python
import base.args
import datetime
import pytz
import sys
import typing

UTC = pytz.utc
timezone = pytz.timezone


def parse_day(s: str) -> datetime.datetime:
  return UTC.localize(datetime.datetime.strptime(s, '%Y-%m-%d'))


def parse_time(s: str) -> datetime.datetime:
  return UTC.localize(datetime.datetime.strptime(s, '%Y-%m-%d %H:%M:%S'))


def format_time(d: datetime.datetime, show_tz_info: bool = False) -> str:
  template = '%Y-%m-%d %H:%M:%S'
  if show_tz_info:
    tzname = d.tzname()
    template += f' {tzname}%z'
  return d.strftime(template)


def is_naive(moment: datetime.datetime) -> bool:
  return moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None

def utc_now() -> datetime.datetime:
  return datetime.now(tz=UTC)

DAY_START_TIME = datetime.time(3, 00)

def day_start(moment: datetime.datetime) -> datetime.datetime:
  if is_naive(moment):
    raise RuntimeError('can\'t process naive datetime.datetime')
  return datetime.combine(moment.date(), DAY_START_TIME, moment.tzinfo)

def parse_moment(s: str) -> datetime.datetime:
  if s == 'now':
    return utc_now()
  try:
    return parse_time(s)
  except:
    pass
  try:
    return parse_day(s)
  except:
    pass
  raise ValueError('invalid moment format')


PAST = parse_day('1988-01-01')
FUTURE = parse_day('2188-01-01')


def format_now(show_tz_info: bool, zone: str | None) -> str:
  now = utc_now()

  if zone is not None:
    now = now.astimezone(timezone(zone))

  return format_time(now, show_tz_info=show_tz_info)


def print_current_time(show_tz_info: bool, zone: str | None) -> None:
  current_time = format_now(show_tz_info, zone)
  print(current_time)


def main(argv):
  parser = base.args.Parser(description='print current time')
  parser.add_argument('-z', dest='show_tz_info', help='show tz info', action='store_true')
  parser.add_argument('--zone', help='convert time to the timezone')
  args = parser.parse_args(argv[1:])
  return print_current_time(**vars(args))


if __name__ == '__main__':
  sys.exit(main(sys.argv))

date : typing.Type = datetime.date
time : typing.Type = datetime.time
timedelta : typing.Type = datetime.timedelta
datetime : typing.Type = datetime.datetime

