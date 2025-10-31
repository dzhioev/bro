#!/usr/bin/env python
import datetime
import pytz

def _parse_date(date: str) -> datetime.datetime:
  return pytz.utc.localize(datetime.datetime.strptime(date, '%Y-%m-%d'))

PAST = _parse_date('1988-01-01')
FUTURE = _parse_date('2188-01-01')

