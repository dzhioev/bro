#!/usr/bin/env python
import pytz
from datetime import datetime
import sys
import base.args

UTC = pytz.utc
timezone = pytz.timezone


def parse_day(s: str) -> datetime:
  return UTC.localize(datetime.strptime(s, '%Y-%m-%d'))


def parse_time(s: str) -> datetime:
  return UTC.localize(datetime.strptime(s, '%Y-%m-%d %H:%M:%S'))


def format_time(d: datetime, show_tz_info: bool = False) -> str:
  template = '%Y-%m-%d %H:%M:%S'
  if show_tz_info:
    tzname = d.tzname()
    template += f' {tzname}%z'
  return d.strftime(template)


def utc_now() -> datetime:
  return datetime.now(tz=UTC)


def parse_moment(s: str) -> datetime:
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
