"""JSON records for option values, requests, and authority expressions."""

from collections.abc import Mapping
from typing import Any

from .authority import Authority, Entry, LazyEntry, Pair
from .patterns import Descriptor, PatternSet
from .rules import Request
from .syntax import Syntax, parse_pattern, parse_permission, render_pattern, render_permission
from .values import Value


def dump_values(values: Mapping[str, Value]) -> dict[str, Any]:
  return {
    name: sorted(value) if isinstance(value, frozenset) else value for name, value in values.items()
  }


def load_values(values: Mapping[str, Any]) -> dict[str, Value]:
  result = {}
  for name, value in values.items():
    if isinstance(value, list):
      if any(type(member) is not str for member in value) or len(set(value)) != len(value):
        raise ValueError(f'invalid recorded subset for {name}')
      result[name] = frozenset(value)
    elif type(value) in (str, bool, int):
      result[name] = value
    else:
      raise ValueError(f'invalid recorded value for {name}')
  return result


def dump_authority(authority: Authority, descriptor: Descriptor) -> dict[str, Any]:
  return {
    'operation': authority.operation,
    'entries': [
      {
        'permission': render_permission(entry.permission),
        'use': sorted({render_pattern(pattern) for pattern in pair.use.patterns}),
        'give': sorted({render_pattern(pattern, recipient=True) for pattern in pair.give.patterns}),
      }
      for entry in authority.entries
      for pair in (entry.resolve(descriptor) if isinstance(entry, LazyEntry) else entry.pair,)
    ],
    'operands': [dump_authority(operand, descriptor) for operand in authority.operands],
  }


def load_authority(value: Mapping[str, Any], syntax: Syntax) -> Authority:
  if set(value) != {'operation', 'entries', 'operands'}:
    raise ValueError('invalid recorded authority')
  return Authority(
    tuple(
      Entry(
        syntax.permission(parse_permission(entry['permission'])),
        Pair(
          PatternSet(tuple(syntax.pattern(parse_pattern(pattern)) for pattern in entry['use'])),
          PatternSet(
            tuple(
              syntax.pattern(parse_pattern(pattern), recipient=True) for pattern in entry['give']
            )
          ),
        ),
      )
      for entry in value['entries']
    ),
    value['operation'],
    tuple(load_authority(operand, syntax) for operand in value['operands']),
  )


def dump_request(request: Request, descriptor: Descriptor) -> dict[str, Any]:
  return {
    'type': request.type,
    'values': dump_values(request.values),
    'gift': dump_authority(request.gift, descriptor),
    'cap': dump_authority(request.cap, descriptor),
  }


def load_request(value: Mapping[str, Any], syntax: Syntax) -> Request:
  if set(value) != {'type', 'values', 'gift', 'cap'}:
    raise ValueError('invalid recorded request')
  return Request(
    value['type'],
    load_values(value['values']),
    load_authority(value['gift'], syntax),
    load_authority(value['cap'], syntax),
  )
