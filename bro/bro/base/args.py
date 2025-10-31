#!/usr/bin/env python

import argparse
from base.time_util import _parse_date

def date_parser(arg):
    try:
        return _parse_date(arg)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid date format: {arg}. Use YYYY-MM-DD.")

