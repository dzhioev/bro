"""The registered webview worker type and its container specification."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import PurePosixPath
from typing import Any, override

from bro.worker_types import Container, LaunchDenied, LaunchRequest, WorkerContainer, WorkerType

WEBVIEW = 'webview'
VNC_PERMIT = 'webview.vnc'
ARTIFACT_VIEW = PurePosixPath('/workspace/artifacts')
VNC_PORT = 6080


@dataclass(frozen=True)
class WebviewFacts:
  vnc: bool
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
  permits = frozenset({'vnc'})
  default_timeout = None
  widens_talk = False
  manual = False

  def __init__(self, host):
    super().__init__(host)
    self._live_by_owner: dict[str, str] = {}

  @override
  def talk(self, request: LaunchRequest) -> Any:
    return frozenset({'owner.question', 'worker.say'})

  @override
  def launch(self, request: LaunchRequest) -> Container:
    unknown = sorted(set(request.args) - {'vnc', 'allowed_origins', 'blocked_origins'})
    if len(unknown) > 0:
      raise LaunchDenied(f'unknown webview field(s): {", ".join(unknown)}')
    vnc = request.args.get('vnc', False)
    if not isinstance(vnc, bool):
      raise LaunchDenied("webview 'vnc' must be a boolean")
    allowed_origins = _origins(request.args, 'allowed_origins')
    blocked_origins = _origins(request.args, 'blocked_origins')
    if vnc and VNC_PERMIT not in request.owner.permits:
      held = ', '.join(f':{permit}' for permit in sorted(request.owner.permits)) or '(none)'
      raise LaunchDenied(f'the VNC view needs :{VNC_PERMIT}; permits held: {held}')
    if request.owner.type == WEBVIEW:
      raise LaunchDenied('a webview cannot open another webview')
    live = self._live_by_owner.get(request.owner.mission)
    if live is not None:
      raise LaunchDenied(
        f'this session already holds a live webview {live}; close or cancel it first'
      )

    facts = WebviewFacts(vnc, allowed_origins, blocked_origins)
    options = json.dumps(
      {
        'allowed_origins': list(allowed_origins),
        'blocked_origins': list(blocked_origins),
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
        published_ports=(VNC_PORT,) if vnc else (),
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
      'allowed_origins': list(extension.allowed_origins),
      'blocked_origins': list(extension.blocked_origins),
    }

  @override
  def subscribers(self):
    def track(event, record) -> None:
      if event.type != WEBVIEW or record.parent is None:
        return
      if event.transition == 'accepted':
        self._live_by_owner[record.parent] = record.mission_id
      elif (
        event.transition == 'ended' and self._live_by_owner.get(record.parent) == record.mission_id
      ):
        del self._live_by_owner[record.parent]

    return (track,)
