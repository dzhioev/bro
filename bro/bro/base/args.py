#!/usr/bin/env python

import argparse
from datetime import datetime

def date_parser(arg):
    try:
        return datetime.strptime(arg, '%Y-%m-%d')
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid date format: {arg}. Use YYYY-MM-DD.")


        
