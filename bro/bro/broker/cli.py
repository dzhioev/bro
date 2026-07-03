#!/usr/bin/env python
"""`broker` CLI — send and receive broker messages from inside a peer.

The channel comes from `BROKER_CHANNEL` (`Client.from_env`); with it unset every
subcommand is inert — a stderr note and exit 0 — so scripts embedded in a peer
work unchanged where there is no broker. Message output on stdout is the wire
JSON, one object per line.
"""

import json
import sys
from typing import Any, Optional

import base.args
from base import log
from broker.client import CHANNEL_ENV, Client

__cli_name__ = 'broker'


def _payload(arg: str) -> dict[str, Any]:
  try:
    parsed = json.loads(arg)
  except json.JSONDecodeError as e:
    raise base.args.ArgumentTypeError(f'payload is not valid JSON: {e}')
  if not isinstance(parsed, dict):
    raise base.args.ArgumentTypeError(f'payload must be a JSON object, got {type(parsed).__name__}')
  return parsed


def _send(type: str, payload: dict[str, Any]) -> int:
  client = Client.from_env()
  if client is None:
    log.info(f'broker: {CHANNEL_ENV} unset, message not sent')
    return 0
  try:
    client.send(type, payload)
  finally:
    client.close()
  return 0


def _request(type: str, payload: dict[str, Any], timeout: Optional[float]) -> int:
  client = Client.from_env()
  if client is None:
    log.info(f'broker: {CHANNEL_ENV} unset, request not sent')
    return 0
  try:
    reply = client.request(type, payload, timeout)
  except (TimeoutError, ConnectionError) as e:
    log.error(str(e))
    return 1
  finally:
    client.close()
  sys.stdout.write(reply.to_bytes().decode('utf-8') + '\n')
  return 0


def _receive(timeout: Optional[float]) -> int:
  client = Client.from_env()
  if client is None:
    log.info(f'broker: {CHANNEL_ENV} unset, nothing to receive')
    return 0
  try:
    message = client.receive(timeout)
  finally:
    client.close()
  if message is None:
    return 1
  sys.stdout.write(message.to_bytes().decode('utf-8') + '\n')
  return 0


def main(argv: list[str]) -> Optional[int]:
  parser = base.args.Parser(description='send and receive broker messages from inside a peer')
  sub = parser.add_subparsers(dest='command')

  send_parser = sub.add_parser('send', help='send a fire-and-forget message')
  send_parser.add_argument('type', help='message-type tag')
  send_parser.add_argument(
    'payload', nargs='?', type=_payload, default={}, help='JSON object payload (default: {})'
  )
  send_parser.set_handler(_send)

  request_parser = sub.add_parser(
    'request', help='send a typed request and print the correlated reply'
  )
  request_parser.add_argument('type', help='message-type tag')
  request_parser.add_argument(
    'payload', nargs='?', type=_payload, default={}, help='JSON object payload (default: {})'
  )
  request_parser.add_argument(
    '--timeout',
    type=float,
    help='seconds to wait for the reply (default: wait indefinitely); exit 1 on timeout',
  )
  request_parser.set_handler(_request)

  receive_parser = sub.add_parser(
    'receive', help='receive one message and print it; exit 1 when nothing arrives'
  )
  receive_parser.add_argument(
    '--timeout', type=float, help='seconds to wait for a message (default: wait indefinitely)'
  )
  receive_parser.set_handler(_receive)

  return parser.dispatch(argv)
