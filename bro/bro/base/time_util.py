#!/usr/bin/env python
import base.args

from icecream import ic

import pytz
import sys
import logging
import datetime as dt

UTC = pytz.utc
timezone = pytz.timezone


def parse_day(s: str) -> dt.datetime:
  return UTC.localize(dt.datetime.strptime(s, '%Y-%m-%d'))


def parse_time(s: str) -> dt.datetime:
  return UTC.localize(datetime.strptime(s, '%Y-%m-%d %H:%M:%S'))


def format_time(d: dt.datetime, show_tz_info: bool = False) -> str:
  template = '%Y-%m-%d %H:%M:%S'
  if show_tz_info:
    tzname = d.tzname()
    template += f' {tzname}%z'
  return d.strftime(template)


def is_naive(moment: dt.datetime) -> bool:
  return moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None


def utc_now() -> dt.datetime:
  return datetime.now(tz=UTC)


DAY_START_TIME = dt.time(3, 00)


def day_start(moment: dt.datetime) -> dt.datetime:
  if is_naive(moment):
    raise RuntimeError("can't process naive datetime.datetime")
  return datetime.combine(moment.date(), DAY_START_TIME, moment.tzinfo)


def parse_moment(s: str) -> dt.datetime:
  logging.debug(f'> parse_moment("{s}")')
  if s == 'now':
    return utc_now()
  try:
    return parse_time(s)
  except Exception as e:
    logging.debug(f'"{s}": {e})')

  try:
    return parse_day(s)
  except Exception as e:
    logging.debug(f'"{s}": {e})')

  raise ValueError(f'failed to parse moment: "{s}"')


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

time = dt.datetime
date = dt.date
datetime = dt.datetime
timedelta = dt.timedelta
