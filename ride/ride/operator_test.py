import contextlib
import json
import os
import signal
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from bro.broker import brotocol
from bro.broker.dispatcher import Broker, Dispatcher
from bro.broker.transport import Provisioned
from bro.broker.transports.tcp import Endpoint
from bro.workspace.paths import runtime_base
from ride import cli, operator
from ride.authority_test_helper import mission
from ride.broker_root import run_root_via_broker
from ride.peer_facts import PeerFacts, WorkerFacts
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace
from ride.workspace.spawn import ProcessLaunchSpec


def _record() -> operator.ControlRecord:
  return operator.ControlRecord('tcp://private-token@127.0.0.1:1234', 'a' * 64)


def _path(name='root') -> Path:
  return runtime_base() / 'control' / f'{name}.json'


def test_control_is_private_and_removed_on_normal_and_exceptional_exit():
  for fails in (False, True):
    with pytest.raises(RuntimeError) if fails else contextlib.nullcontext():
      with operator.publish_control('root', _record()):
        assert stat.S_IMODE(_path().stat().st_mode) == 0o600
        assert operator.read_control('root') == _record()
        assert operator.clean_controls() == (0, 1)
        if fails:
          raise RuntimeError('root failed')
    assert not _path().exists()


def test_live_control_cannot_be_replaced():
  with operator.publish_control('root', _record()):
    with pytest.raises(RuntimeError, match='active operator channel'):
      with operator.publish_control('root', _record()):
        pytest.fail('replaced live root')
    assert operator.read_control('root') == _record()


def test_clean_sweeps_a_hard_killed_roots_record():
  script = textwrap.dedent("""
    import os
    import signal
    from ride.operator import ControlRecord, publish_control
    with publish_control('killed', ControlRecord('tcp://token@127.0.0.1:1234', 'runtime')):
      os.kill(os.getpid(), signal.SIGKILL)
  """)
  process = subprocess.run([sys.executable, '-c', script], env=os.environ, check=False)
  assert process.returncode == -signal.SIGKILL
  assert operator.clean_controls(dry_run=True) == (1, 0)
  assert _path('killed').exists()
  assert operator.clean_controls() == (1, 0)
  assert not _path('killed').exists()


@pytest.mark.parametrize('root', ['missing', 'before-switch'])
def test_root_without_control_is_refused_by_name(root, capsys):
  with pytest.raises(SystemExit):
    cli.main(['ride', 'missions', root])
  assert f'root {root!r} has no operator control file' in capsys.readouterr().err


def test_client_reexecutes_before_talking_to_root(monkeypatch):
  calls = []
  argv = ['ride', 'missions', 'root']
  with operator.publish_control('root', _record()):
    monkeypatch.setattr(cli, 'reexec_from_runtime', lambda *args: calls.append(('runtime', args)))
    monkeypatch.setattr(operator, 'list_missions', lambda root: calls.append(('list', root)) or 0)
    assert cli.main(argv) == 0
  assert calls == [('runtime', (_record().runtime, argv)), ('list', 'root')]


@pytest.mark.parametrize('exit_code', [0, 7])
def test_root_provisions_operator_channel_before_spawning_and_removes_it(
  tmp_path, monkeypatch, exit_code
):
  monkeypatch.setattr('ride.broker_root.broker_bind_hosts', lambda: ['127.0.0.1'])
  workspace = Workspace.ensure('root', None, Isolation.UNBOXED)
  output = tmp_path / 'observed.json'
  script = textwrap.dedent("""
    import json
    import os
    import sys
    from bro.broker.client import Client
    from bro.broker.transport import connect
    from ride.operator import MISSIONS, read_control
    with Client(connect(os.environ['BROKER_UPSTREAM'])) as mission:
      refused = mission.call(MISSIONS, {}, timeout=5).payload
    with Client(connect(read_control('root').address)) as operator:
      listed = operator.call(MISSIONS, {}, timeout=5).payload
    with open(sys.argv[1], 'w') as stream:
      json.dump({'refused': refused, 'listed': listed}, stream)
    sys.exit(int(sys.argv[2]))
  """)
  runtime = MagicMock(reference=_record().runtime)
  code = run_root_via_broker(
    ProcessLaunchSpec(
      [sys.executable, '-c', script, str(output), str(exit_code)],
      str(tmp_path),
      dict(os.environ),
      interactive=False,
    ),
    workspace=workspace,
    bro='bro',
    mission=mission(),
    container_runtime=MagicMock(),
    runtime_bundle=runtime,
    types={},
  )
  assert code == exit_code
  observed = json.loads(output.read_text())
  assert observed['refused']['outcome'] == 'denied'
  assert 'operator channel required' in observed['refused']['error']
  assert observed['listed']['outcome'] == 'ok'
  assert observed['listed']['value']['missions'][0]['name'] == workspace.name
  assert not _path().exists()


@pytest.mark.asyncio
async def test_operator_guard_applies_to_every_registered_operator_request(tmp_path):
  handlers = {}
  broker = MagicMock(spec=Broker)
  broker.on.side_effect = lambda kind, handler: handlers.__setitem__(kind, handler)
  peers = PeerFacts(WorkerFacts('bro', 'root'), root_tree=tmp_path, root_path=tmp_path)
  control = operator.OperatorControl(broker, root='root', runtime=_record().runtime, peers=peers)
  reached = []

  def handler(context, peer, message):
    reached.append(message.kind)
    context.reply(peer, {'outcome': 'ok'})

  kinds = ('operator.commit', 'operator.claim', 'operator.override')
  for kind in kinds:
    control.on(kind, handler)
  dispatcher = Dispatcher()
  runtime = MagicMock()
  runtime.provision = AsyncMock(return_value=Provisioned('operator', Endpoint(1234, 'secret')))
  runtime.close = AsyncMock()
  dispatcher.bind(runtime)
  context = MagicMock()
  async with control.lifetime(dispatcher):
    for kind in (operator.MISSIONS, *kinds):
      handlers[kind](context, 'mission', brotocol.request(kind, {}))
      assert context.reply.call_args.args[1]['outcome'] == 'denied'
    assert not reached
    for kind in kinds:
      handlers[kind](context, 'operator', brotocol.request(kind, {}))
      assert context.reply.call_args.args[1]['outcome'] == 'ok'
  assert reached == list(kinds)
  runtime.close.assert_awaited_once_with('operator')


@pytest.mark.asyncio
async def test_listing_paginates_at_the_wire_bound_and_client_collects_every_page(
  monkeypatch, capsys
):
  from bro.broker.client import Client

  handlers = {}
  broker = MagicMock(spec=Broker)
  broker.on.side_effect = lambda kind, handler: handlers.__setitem__(kind, handler)
  peers = MagicMock(spec=PeerFacts)
  rows = [{'id': str(index), 'name': 'x' * 200} for index in range(12)]
  peers.missions.return_value = rows
  control = operator.OperatorControl(broker, root='root', runtime=_record().runtime, peers=peers)
  dispatcher = Dispatcher()
  runtime = MagicMock()
  runtime.provision = AsyncMock(return_value=Provisioned('operator', Endpoint(1234, 'secret')))
  runtime.close = AsyncMock()
  dispatcher.bind(runtime)
  observed = []
  monkeypatch.setattr(brotocol, 'MAX_FRAME_BYTES', 1024)

  def call(self, kind, args, timeout):
    context = MagicMock()
    request = brotocol.request(kind, args)
    handlers[kind](context, 'operator', request)
    payload = context.reply.call_args.args[1]
    reply = brotocol.Message(type=brotocol.Tag.RESULT, request=request.request_id, payload=payload)
    assert len(reply.to_bytes()) <= brotocol.MAX_FRAME_BYTES
    observed.append(payload)
    return reply

  monkeypatch.setattr(Client, 'call', call)
  monkeypatch.setattr('bro.broker.transport.connect', lambda _address: MagicMock())
  async with control.lifetime(dispatcher):
    assert operator.list_missions('root') == 0
  assert len(observed) > 1
  assert 'cursor' in observed[0]['value']
  assert 'cursor' not in observed[-1]['value']
  assert json.loads(capsys.readouterr().out) == rows


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'args', [{'cursor': -1}, {'cursor': True}, {'cursor': 'bad'}, {'extra': 1}, {'cursor': 2}]
)
async def test_invalid_listing_arguments_are_refused(args):
  handlers = {}
  broker = MagicMock(spec=Broker)
  broker.on.side_effect = lambda kind, handler: handlers.__setitem__(kind, handler)
  peers = MagicMock(spec=PeerFacts)
  peers.missions.return_value = []
  control = operator.OperatorControl(broker, root='root', runtime=_record().runtime, peers=peers)
  dispatcher = Dispatcher()
  runtime = MagicMock()
  runtime.provision = AsyncMock(return_value=Provisioned('operator', Endpoint(1234, 'secret')))
  runtime.close = AsyncMock()
  dispatcher.bind(runtime)
  async with control.lifetime(dispatcher):
    context = MagicMock()
    handlers[operator.MISSIONS](context, 'operator', brotocol.request(operator.MISSIONS, args))
    assert context.reply.call_args.args[1]['outcome'] == 'denied'


@pytest.mark.parametrize('error', [ConnectionError('closed'), TimeoutError('silent')])
def test_joint_start_requests_report_an_unavailable_operator_channel(monkeypatch, error):
  monkeypatch.setattr(
    operator,
    'read_control',
    lambda root: operator.ControlRecord('tcp://token@localhost:1', '/runtime'),
  )

  def connect(address):
    raise error

  monkeypatch.setattr('bro.broker.transport.connect', connect)
  with pytest.raises(RuntimeError, match='operator channel is unavailable for operator.claim'):
    operator._request(
      'root', 'operator.claim', {'token': 'child', 'commit': 'committed', 'workspace': 'child'}
    )
