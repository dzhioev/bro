#!/usr/bin/env python
"""`broker` CLI — send broker requests and receive messages from inside a peer.

The client comes from `Client.from_env`.
With both broker variables unset every subcommand is inert, so scripts embedded in a peer work unchanged where no broker was intended.
A leftover `BROKER_UPSTREAM` reports that the session proxy failed at launch.
Message output on stdout is the wire JSON, one object per line.
"""

import json
import sys
from typing import Any, Optional

import bro.base.args as base_args
from bro.base import log
from bro.broker.client import CHANNEL_ENV, Client

__cli_name__ = 'broker'


def _args(arg: str) -> dict[str, Any]:
  try:
    parsed = json.loads(arg)
  except json.JSONDecodeError as e:
    raise base_args.ArgumentTypeError(f'args is not valid JSON: {e}')
  if not isinstance(parsed, dict):
    raise base_args.ArgumentTypeError(f'args must be a JSON object, got {type(parsed).__name__}')
  return parsed


def _send(kind: str, args: dict[str, Any]) -> int:
  client = Client.from_env()
  if client is None:
    log.info(f'broker: {CHANNEL_ENV} unset, request not sent')
    return 0
  with client:
    client.send(kind, args)
  return 0


def _request(kind: str, args: dict[str, Any], timeout: Optional[float]) -> int:
  client = Client.from_env()
  if client is None:
    log.info(f'broker: {CHANNEL_ENV} unset, request not sent')
    return 0
  with client:
    try:
      reply = client.request(kind, args, timeout)
    except (TimeoutError, ConnectionError) as e:
      log.error(str(e))
      return 1
  sys.stdout.write(reply.to_bytes().decode('utf-8') + '\n')
  return 0


def _receive(timeout: Optional[float]) -> int:
  client = Client.from_env()
  if client is None:
    log.info(f'broker: {CHANNEL_ENV} unset, nothing to receive')
    return 0
  with client:
    message = client.receive(timeout)
  if message is None:
    return 1
  sys.stdout.write(message.to_bytes().decode('utf-8') + '\n')
  return 0


def main(argv: list[str]) -> Optional[int]:
  parser = base_args.Parser(description='send broker requests and receive messages from inside a peer')  # fmt: skip
  subparsers = parser.add_subparsers(dest='command')

  send_parser = subparsers.add_parser('send', help='send a request without awaiting its reply')
  send_parser.add_argument('kind', help='the kind the request names')
  send_parser.add_argument(
    'args', nargs='?', type=_args, default={}, help='JSON object kind arguments (default: {})'
  )
  send_parser.set_handler(_send)

  request_parser = subparsers.add_parser(
    'request', help='send a request and print the correlated reply'
  )
  request_parser.add_argument('kind', help='the kind the request names')
  request_parser.add_argument(
    'args', nargs='?', type=_args, default={}, help='JSON object kind arguments (default: {})'
  )
  request_parser.add_argument(
    '--timeout',
    type=float,
    help='seconds to wait for the reply (default: wait indefinitely); exit 1 on timeout',
  )
  request_parser.set_handler(_request)

  receive_parser = subparsers.add_parser(
    'receive', help='receive one message and print it; exit 1 when nothing arrives'
  )
  receive_parser.add_argument(
    '--timeout', type=float, help='seconds to wait for a message (default: wait indefinitely)'
  )
  receive_parser.set_handler(_receive)

  return parser.dispatch(argv)
