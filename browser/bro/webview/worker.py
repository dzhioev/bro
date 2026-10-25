"""The registered webview worker type and its container specification."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import PurePosixPath
from typing import Any, cast, override

from bro.authority.patterns import Descriptor
from bro.authority.rules import Declaration, Mission, Option
from bro.authority.values import (
  Finite,
  IntegerInterval,
  Integers,
  Scalars,
  StringGrammar,
  Strings,
)
from bro.base.credentials import INSTANCE_CHARACTERS
from bro.worker_types import (
  Container,
  LaunchDenied,
  LaunchRequest,
  OptionValues,
  WorkerContainer,
  WorkerType,
)

WEBVIEW = 'webview'
ARTIFACT_VIEW = PurePosixPath('/workspace/artifacts')
VNC_PORT = 6080


def vnc_url(host_port: int) -> str:
  return f'http://127.0.0.1:{host_port}/vnc.html?autoconnect=1&resize=scale'


@dataclass(frozen=True)
class WebviewFacts:
  vnc: bool
  vnc_port: int | None
  downloads: bool
  allowed_origins: tuple[str, ...]
  blocked_origins: tuple[str, ...]


def _origins(args: dict[str, Any], name: str) -> tuple[str, ...]:
  value = args.get(name, [])
  if not isinstance(value, list) or any(
    not isinstance(origin, str) or len(origin) == 0 or ';' in origin for origin in value
  ):
    raise LaunchDenied(f"webview '{name}' must be a list of non-empty strings without ';'")
  return tuple(value)


def _dockerfile() -> bytes:
  return resources.files('bro.webview').joinpath('container', 'Dockerfile').read_bytes()


class WebviewType(WorkerType):
  name = WEBVIEW
  default_timeout = None
  widens_talk = False
  manual = False

  @classmethod
  def authority_declaration(cls, credential_kinds: frozenset[str]) -> Declaration:
    sentinels = frozenset({'auto'})
    none = frozenset({'none'})

    def defaults(values: Descriptor, reading: object):
      return {'vnc_port': 'auto', 'timeout': 'none'}

    def validate(descriptor: Descriptor) -> None:
      if descriptor.values['vnc_port'] != 'auto' and descriptor.values.get('vnc') is not True:
        raise ValueError('vnc_port requires vnc')

    return Declaration(
      {
        'vnc': Option('scalar', Finite(frozenset({True}), frozenset({True}))),
        'downloads': Option('scalar', Finite(frozenset({True}), frozenset({True}))),
        'vnc_port': Option(
          'scalar',
          Scalars((Integers((IntegerInterval(1024, 65535),)), Finite(sentinels, sentinels))),
          open=True,
        ),
        'cred.cookies': Option(
          'scalar',
          Strings(
            cofinite=True,
            grammar=StringGrammar(INSTANCE_CHARACTERS, INSTANCE_CHARACTERS, empty=True),
          ),
        ),
        'timeout': Option(
          'scalar',
          Scalars((Integers((IntegerInterval(1, None),)), Finite(none, none))),
          open=True,
        ),
      },
      defaults=defaults,
      validate=validate,
    )

  @classmethod
  def option_values(cls, fields: Mapping[str, Any], reading: object) -> OptionValues:
    options = {'vnc', 'vnc_port', 'downloads', 'timeout', 'cred.cookies'}
    content_names = {'allowed_origins', 'blocked_origins', 'share'}
    if fields.keys() - options - content_names - {'pass', 'cookies'}:
      raise ValueError('unknown webview fields')
    values = {name: value for name, value in fields.items() if name in options}
    for flag in ('vnc', 'downloads'):
      if flag in values:
        if type(values[flag]) is not bool:
          raise ValueError(f'{flag} must be a flag')
        if values[flag] is False:
          del values[flag]
    for name, sentinel in (('vnc_port', 'auto'), ('timeout', 'none')):
      if name in values and values[name] is None:
        values[name] = sentinel
    if 'cookies' in fields:
      values['cred.cookies'] = fields['cookies']
    if 'pass' in fields:
      from bro.base.credentials import parse_picks

      picks = parse_picks(fields['pass'], where='webview', field='pass')
      if picks.keys() - {'cookies'}:
        raise ValueError('webview accepts cookies only')
      if 'cookies' in picks:
        if 'cred.cookies' in values:
          raise ValueError('cookies is set twice')
        values['cred.cookies'] = picks['cookies']
    return OptionValues(
      values, {name: value for name, value in fields.items() if name in content_names}
    )

  @override
  def talk(self, request: LaunchRequest) -> Any:
    return frozenset({'owner.question', 'worker.say'})

  @override
  def launch(self, request: LaunchRequest) -> Container:
    if not isinstance(request.record, Mission):
      raise ValueError('webview start requires an admitted record')
    values = request.record.descriptor.values
    vnc = values.get('vnc') is True
    downloads = values.get('downloads') is True
    vnc_port = None if values['vnc_port'] == 'auto' else cast(int, values['vnc_port'])
    allowed_origins = _origins(request.args, 'allowed_origins')
    blocked_origins = _origins(request.args, 'blocked_origins')
    facts = WebviewFacts(vnc, vnc_port, downloads, allowed_origins, blocked_origins)
    options = json.dumps(
      {
        'allowed_origins': list(allowed_origins),
        'blocked_origins': list(blocked_origins),
        'downloads': downloads,
        'vnc': vnc,
      },
      separators=(',', ':'),
      sort_keys=True,
    )
    return Container(
      WorkerContainer(
        files={'Dockerfile': _dockerfile()},
        command=('webview', 'serve'),
        env={'WEBVIEW_OPTIONS': options},
        published_ports={VNC_PORT: vnc_port} if vnc else {},
        artifact_view=ARTIFACT_VIEW,
      ),
      extension=facts,
    )

  @override
  def audit_fields(self, extension: Any) -> dict[str, Any]:
    if not isinstance(extension, WebviewFacts):
      raise TypeError('webview audit fields need WebviewFacts')
    return {
      'vnc': extension.vnc,
      'vnc_port': extension.vnc_port,
      'downloads': extension.downloads,
      'allowed_origins': list(extension.allowed_origins),
      'blocked_origins': list(extension.blocked_origins),
    }
