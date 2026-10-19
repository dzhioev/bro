import asyncio
import contextlib
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from bro.authority.records import dump_request
from bro.base import credential_apply, credentials
from bro.base.credential_environment import overlay
from bro.broker import brotocol
from bro.broker.journal import Journal
from bro.monitor import party_member_dir
from ride import credential_overrides
from ride.authority_launch import mission_view
from ride.authority_test_helper import mission, reading
from ride.credential_applier import CredentialApplier
from ride.peer_facts import PeerFacts, WorkerFacts
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace


class Context:
  def __init__(self):
    self.response = None
    self.answer = None

  def reply(self, peer, payload):
    self.response = payload

  def defer(self, peer, answer):
    self.answer = asyncio.create_task(answer)

  async def result(self):
    result = self.response if self.answer is None else await self.answer
    assert result is not None
    return result


@pytest.fixture
def running(tmp_path, monkeypatch):
  workspace = Workspace.ensure('credential-root', None, Isolation.UNBOXED)
  source = tmp_path / 'source'
  (source / 'creds').mkdir(parents=True)
  for name, value in {
    'brave+one': '{"api_key":"first"}',
    'brave+two': '{"api_key":"second"}',
    'github+one': 'first-git',
    'github+two': 'second-git',
    'aws+one': 'unchanged',
  }.items():
    (source / 'creds' / (name + '.cred')).write_text(value)
  monkeypatch.setattr(credentials, 'STORE_DIR', str(source))
  reading_value = reading()
  original = mission(source=reading_value, values={'pass': ['brave+one', 'github+one', 'aws+one']})
  files, kinds = credentials.build_scoped_store(
    credentials.Store(
      credentials.default_registry(), source, {'brave': 'one', 'github': 'one', 'aws': 'one'}
    ),
    ['brave', 'github', 'aws'],
  )
  store = tmp_path / 'session'
  for filename, content in files.items():
    credential_apply.atomic_file(store / filename, content)
  environment = {
    **os.environ,
    'BRO_STORE': str(store),
    'BRO_INSTALL_DIR': str(tmp_path / 'environment'),
    'BRO_INSTALL_KINDS': ' '.join(sorted(kinds)),
  }
  directed = credentials.Store(credentials.default_registry(), store, {})
  environment.update(
    credentials.install_hooks(
      directed.registry, kinds, directed, tmp_path / 'environment', environment
    )
  )
  facts = WorkerFacts(
    'bro', workspace.name, record=original, credential_applier=CredentialApplier(environment)
  )
  peers = PeerFacts(facts, root_tree=workspace.tree, root_path=workspace.path)
  journal = Journal()
  peers.bind_journal(journal)
  journal.subscribe(peers.observe_journal)
  record = journal.open('root-id', 'root', None, None, {}, type='bro')
  journal.bind(record, 'worker')
  journal.started(record)
  workspace.resume_file.write_text(
    json.dumps({'authority_request': dump_request(original.request, original.descriptor)})
  )
  manager = credential_overrides.CredentialOverrides(peers, journal, reading_value)
  journal.subscribe(manager.observe)
  return SimpleNamespace(
    workspace=workspace,
    source=reading_value,
    original=original,
    facts=facts,
    peers=peers,
    journal=journal,
    record=record,
    manager=manager,
    store=directed,
    environment=environment,
  )


def change(running, *changes, name=None):
  context = Context()
  message = brotocol.request(
    'operator.override',
    {'name': name or running.workspace.name, 'changes': list(changes)},
  )
  running.manager.handle(context, 'operator', message)
  return context


@pytest.mark.asyncio
async def test_live_applier_updates_store_git_and_environment_without_replacing_other_kinds(
  running,
):
  from bro.base import spawn

  unrelated = running.store.material_path('aws')
  inode = unrelated.stat().st_ino
  async with running.manager.lifetime():
    result = await change(running, ('cred', 'github+two'), ('cred', 'brave+two')).result()
  assert result['value']['status'] == 'applied'
  assert running.store.get_json('brave')['api_key'] == 'second'
  assert running.store.get('github') == 'second-git'
  assert 'github+one' not in running.store.instance_names()
  command = spawn.run(
    ['git', 'credential', 'fill'],
    input='protocol=https\nhost=github.com\n\n',
    text=True,
    capture_output=True,
    env=overlay(running.environment),
    timeout=10,
  )
  assert command.returncode == 0, command.stderr
  assert 'password=second-git' in command.stdout
  assert unrelated.stat().st_ino == inode
  assert running.store.get('aws') == 'unchanged'
  saved = json.loads(running.workspace.resume_file.read_text())
  assert saved['authority_request']['values']['cred.github'] == 'two'
  assert saved['override_operation']['state'] == 'applied'
  assert running.record.overrides[-1]['generation'] == running.store.generation
  assert running.facts.pending_override is None


@pytest.mark.asyncio
async def test_revoke_and_grant_a_held_kind_and_refuse_missing_material(running):
  async with running.manager.lifetime():
    result = await change(running, ('revoke', 'brave')).result()
    assert result['value']['status'] == 'applied'
    assert running.store.try_get('brave') is None
    assert 'brave+one' not in running.store.instance_names()
    result = await change(running, ('cred', 'brave+two')).result()
    assert result['value']['status'] == 'applied'
    assert running.store.get_json('brave')['api_key'] == 'second'
    before = running.store.generation
    result = await change(running, ('cred', 'brave+missing')).result()
    assert result['outcome'] == 'denied'
    assert running.store.generation == before
    assert running.facts.pending_override is None


@pytest.mark.asyncio
@pytest.mark.parametrize('ended', [False, True])
async def test_uncertain_acknowledgement_settles_from_receipt_alive_or_ended(
  running, monkeypatch, ended
):
  monkeypatch.setattr(credential_overrides, 'ACK_TIMEOUT', 0.01)
  entered, release = threading.Event(), threading.Event()
  original = running.facts.credential_applier

  class LostAcknowledgement:
    def apply(self, request, receipt):
      entered.set()
      assert release.wait(10), 'test did not release the applier'
      original.apply(request, receipt)
      raise ConnectionError('acknowledgement lost')

  running.facts.credential_applier = LostAcknowledgement()
  async with running.manager.lifetime():
    context = change(running, ('cred', 'brave+two'))
    assert await asyncio.to_thread(entered.wait, 10)
    result = await context.result()
    assert result['value']['status'] == 'uncertain'
    pending = running.facts.pending_override
    assert pending['id'] in (await change(running, ('cred', 'brave+one')).result())['error']
    assert running.facts.record is running.original
    assert (
      mission_view(running.facts.record)['authority'] == mission_view(running.original)['authority']
    )
    saved = json.loads(running.workspace.resume_file.read_text())
    assert saved['override_operation']['state'] == 'pending'
    if ended:
      running.journal.end(running.record, {'outcome': 'ok'})
    running.facts.credential_applier = original
    release.set()
    await asyncio.wait_for(running.manager._signals['root-id'].wait(), 10)
    assert pending['state'] == 'applied'
    assert running.facts.record.request.values['cred.brave'] == 'two'
    assert running.store.get_json('brave')['api_key'] == 'second'


@pytest.mark.asyncio
@pytest.mark.parametrize('acknowledged', [False, True])
async def test_next_resume_commits_only_a_receipted_operation(running, monkeypatch, acknowledged):
  monkeypatch.setattr(credential_overrides, 'ACK_TIMEOUT', 0.01)
  running.facts.credential_applier = None
  async with running.manager.lifetime():
    result = await change(running, ('cred', 'brave+two')).result()
    operation = result['value']['operation']
    if acknowledged:
      credential_apply.atomic_file(
        credential_overrides.receipt_path(running.workspace.path, operation),
        json.dumps({'operation': operation, 'generation': 1}).encode(),
      )
    credential_overrides.settle_resume(running.workspace.path)
    saved = json.loads(running.workspace.resume_file.read_text())
    assert saved['authority_request']['values']['cred.brave'] == ('two' if acknowledged else 'one')
    assert saved['override_operation']['state'] == ('applied' if acknowledged else 'dropped')


@pytest.mark.asyncio
async def test_joined_member_uses_its_records_without_touching_the_party_resume(running):
  member = party_member_dir(running.workspace.path, 'credential-member')
  member.mkdir(parents=True)
  running.facts.member = 'credential-member'
  saved = running.workspace.resume_file.read_bytes()
  async with running.manager.lifetime():
    result = await change(running, ('cred', 'brave+two'), name='credential-member').result()
    assert result['value']['status'] == 'applied'
  assert running.workspace.resume_file.read_bytes() == saved
  assert (member / 'override.json').is_file()
  assert not (member / 'resume.json').exists()
  saved_receipt = credential_overrides.receipt(member, result['value']['operation'])
  assert saved_receipt is not None and saved_receipt['generation'] == 1


def test_boxed_applier_publishes_as_the_session_user(running, tmp_path, monkeypatch):
  import subprocess

  import ride.credential_applier as appliers

  commands = []

  def run(arguments, **keywords):
    commands.append(arguments)
    output = '{"generation":1}' if 'credentials' in arguments else ''
    return subprocess.CompletedProcess(arguments, 0, output, '')

  monkeypatch.setattr(appliers.spawn, 'run', run)
  monkeypatch.setattr(appliers.subprocess, 'run', run)
  applier = CredentialApplier(running.environment, 'container-id')
  receipt = tmp_path / 'receipt.json'
  assert applier.apply({'operation': 'boxed-operation'}, receipt) == 1
  publish = next(command for command in commands if 'credentials' in command)
  assert publish[publish.index('--user') + 1] == 'ride'
  ownership = next(command for command in commands if 'chown' in command)
  assert 'ride:ride' in ownership
  assert commands.index(ownership) < commands.index(publish)
  assert json.loads(receipt.read_text())['operation'] == 'boxed-operation'


@pytest.mark.asyncio
async def test_member_ending_during_apply_drops_the_pending_operation(running, monkeypatch):
  monkeypatch.setattr(credential_overrides, 'ACK_TIMEOUT', 0.01)
  member = party_member_dir(running.workspace.path, 'ending-member')
  member.mkdir(parents=True)
  running.facts.member = 'ending-member'
  entered, released = threading.Event(), threading.Event()

  class FinishingApplier:
    def apply(self, request, receipt):
      entered.set()
      assert released.wait(10), 'test did not release apply'
      credential_apply.atomic_file(
        receipt, json.dumps({'operation': request['operation'], 'generation': 1}).encode()
      )
      return 1

  running.facts.credential_applier = FinishingApplier()
  async with running.manager.lifetime():
    result = change(running, ('cred', 'brave+two'), name='ending-member')
    assert await asyncio.to_thread(entered.wait, 10)
    assert (await result.result())['value']['status'] == 'uncertain'
    pending = running.facts.pending_override
    running.journal.end(running.record, {'outcome': 'ok'})
    released.set()
    await asyncio.wait_for(running.manager._signals['root-id'].wait(), 10)
    assert pending['state'] == 'dropped'
    assert running.facts.record is running.original
    assert running.record.overrides == []
    assert not (member / 'override.json').exists()


def test_live_probe_setup_runs_against_a_scoped_store(running):
  import sys

  from bro.base import spawn
  from ride.e2e_test import _CREDENTIAL_OVERRIDE_SETUP

  result = spawn.run(
    [
      sys.executable,
      '-P',
      '-c',
      _CREDENTIAL_OVERRIDE_SETUP + '\nassert overlay()["GIT_CONFIG_COUNT"]\n',
    ],
    env={**running.environment, 'RIDE_WORKSPACE': running.workspace.name},
    capture_output=True,
    text=True,
    timeout=15,
  )
  assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize('ended', [False, True])
async def test_manual_receipt_settles_after_the_applier_connection_disappears(
  running, monkeypatch, ended
):
  monkeypatch.setattr(credential_overrides, 'ACK_TIMEOUT', 0.01)
  running.facts.expected = True
  running.facts.credential_applier = None
  async with running.manager.lifetime(), manual_applier(running) as lifeline:
    result = await change(running, ('cred', 'brave+two')).result()
    assert result['value']['status'] == 'uncertain'
    work = Context()
    running.manager.work(work, 'manual-applier', brotocol.request('credentials.work', {}))
    request = (await work.result())['value']
    assert request['operation'] == result['value']['operation']
    credential_apply.atomic_file(
      credential_overrides.receipt_path(running.workspace.path, request['operation']),
      json.dumps({'operation': request['operation'], 'generation': 1}).encode(),
    )
    lifeline.close()
    if ended:
      running.journal.end(running.record, {'outcome': 'ok'})
    await asyncio.wait_for(running.manager._signals['root-id'].wait(), 10)
    assert running.facts.pending_override is None
    assert running.facts.record.request.values['cred.brave'] == 'two'
    saved = json.loads(running.workspace.resume_file.read_text())
    assert saved['override_operation']['state'] == 'applied'


@contextlib.asynccontextmanager
async def manual_applier(running):
  from ride.credential_overrides import LIFETIME_READY, ManualApplier

  path = running.workspace.path / 'applier-lifetime'
  os.mkfifo(path, 0o600)
  descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
  stream = running.manager._channels.enter_context(os.fdopen(descriptor, 'rb', buffering=0))
  running.manager._manual_states['root-id'] = ManualApplier(path, stream)
  running.manager._manual['manual-applier'] = 'root-id'
  with path.open('wb', buffering=0) as lifeline:
    lifeline.write(LIFETIME_READY)
    context = Context()
    running.manager.attach(context, 'manual-applier', brotocol.request('credentials.attach', {}))
    assert (await context.result())['outcome'] == 'ok'
    yield lifeline


@pytest.mark.asyncio
async def test_manual_mission_end_waits_for_the_inflight_receipt(running, monkeypatch):
  import ride.credential_applier as appliers

  monkeypatch.setattr(credential_overrides, 'ACK_TIMEOUT', 0.01)
  applier = running.facts.credential_applier
  running.facts.expected = True
  running.facts.credential_applier = None
  publishing_receipt, release = threading.Event(), threading.Event()
  write = appliers.atomic_file

  def paused_receipt(path, *args, **kwargs):
    if path.parent.name == 'credential-receipts':
      publishing_receipt.set()
      assert release.wait(10), 'test did not release receipt publication'
    return write(path, *args, **kwargs)

  monkeypatch.setattr(appliers, 'atomic_file', paused_receipt)
  async with running.manager.lifetime(), manual_applier(running):
    with contextlib.ExitStack() as cleanup:
      cleanup.callback(release.set)
      result = await change(running, ('cred', 'brave+two')).result()
      assert result['value']['status'] == 'uncertain'
      work = Context()
      running.manager.work(work, 'manual-applier', brotocol.request('credentials.work', {}))
      request = json.loads(Path((await work.result())['value']['request_file']).read_text())
      receipt = credential_overrides.receipt_path(running.workspace.path, request['operation'])
      publication = asyncio.create_task(asyncio.to_thread(applier.apply, request, receipt))
      assert await asyncio.to_thread(publishing_receipt.wait, 10), 'apply did not finish'
      assert running.store.get_json('brave')['api_key'] == 'second'
      assert not receipt.exists()
      running.journal.end(running.record, {'outcome': 'ok'})
      assert running.facts.pending_override['state'] == 'pending'
      release.set()
      assert await asyncio.wait_for(publication, 10) == 1
      await asyncio.wait_for(running.manager._signals['root-id'].wait(), 10)
      saved = json.loads(running.workspace.resume_file.read_text())
      assert saved['override_operation']['state'] == 'applied'
      assert saved['authority_request']['values']['cred.brave'] == 'two'


@pytest.mark.asyncio
async def test_manual_launcher_death_drops_an_ended_operation_without_a_receipt(
  running, monkeypatch
):
  monkeypatch.setattr(credential_overrides, 'ACK_TIMEOUT', 0.01)
  running.facts.expected = True
  running.facts.credential_applier = None
  async with running.manager.lifetime(), manual_applier(running) as lifeline:
    assert (await change(running, ('cred', 'brave+two')).result())['value']['status'] == 'uncertain'
    work = Context()
    running.manager.work(work, 'manual-applier', brotocol.request('credentials.work', {}))
    await work.result()
    running.journal.end(running.record, {'outcome': 'ok'})
    assert running.facts.pending_override is not None
    lifeline.close()
    await asyncio.wait_for(running.manager._signals['root-id'].wait(), 10)
    assert running.facts.pending_override is None
    assert running.facts.record is running.original
    assert (
      json.loads(running.workspace.resume_file.read_text())['override_operation']['state']
      == 'dropped'
    )
