import asyncio
import hashlib
import io
import os
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import replace

import pytest

from bro.files import access, operations, registry
from bro.files.confinement import SandboxLimits
from bro.files.operations import Operations
from bro.files.operations_test_helper import Content, Details, StubRunner
from bro.files.slots import WorkerSlots
from bro.files.types import IDENTIFICATION_BYTES, Limits, Signature, View
from bro.files.types_test_helper import EntryPoint, make_type


@pytest.fixture
def setup(monkeypatch, tmp_path):
  file_type = make_type(
    tmp_path,
    details=Details,
    views=(
      View('text', 'Read page text', Content, True),
      View('whole', 'Read all', Content, False),
    ),
    limits=Limits(10000, 5, 100000, 20),
    provider='bro.files.operations_test_helper:provider',
  )
  monkeypatch.setattr(registry, '_entry_points', lambda: (EntryPoint('sample', file_type),))
  monkeypatch.setattr(operations, 'registry', registry.TypeRegistry())
  runner = StubRunner()
  slots = WorkerSlots(budget_bytes=SandboxLimits().address_space_bytes, queue_seconds=0.01)
  state = Operations(runner, slots=slots)
  path = tmp_path / 'document.unknown'
  path.write_bytes(b'SAMPLE\nhello')
  return state, runner, path, file_type


@pytest.mark.asyncio
async def test_snapshot_and_page_selection_reach_runner_and_results(setup):
  state, runner, path, file_type = setup
  data = path.read_bytes()
  info = await state.info(str(path))
  assert info.size_bytes == len(data)
  assert info.sha256 == hashlib.sha256(data).hexdigest() == info.details['title']
  assert info.type == file_type.name
  assert [view.name for view in info.views] == [view.name for view in file_type.views]
  viewed = await state.view(str(path), 'text', '2,1-2')
  assert viewed.sha256 == info.sha256
  assert viewed.view == 'text'
  assert viewed.content['pages'] == [
    {'page_number': number, 'notes': ['untrusted text'], 'text': data.decode()} for number in [1, 2]
  ]
  assert [job.snapshot for job in runner.jobs] == [data, data]
  assert runner.jobs[1].arguments == {'view': 'text', 'pages': [1, 2]}


@pytest.mark.asyncio
async def test_snapshot_survives_path_change_and_reads_only_once(setup, monkeypatch):
  state, runner, path, _ = setup
  original = runner.run
  data = path.read_bytes()

  async def replace_file(job):
    path.write_bytes(b'changed')
    return await original(job)

  monkeypatch.setattr(runner, 'run', replace_file)
  result = await state.info(str(path))
  assert result.sha256 == hashlib.sha256(data).hexdigest()
  assert runner.jobs[0].snapshot == data


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['fifo', 'device', 'directory', 'missing'])
async def test_nonregular_and_unreadable_paths_are_named_without_workers(setup, kind):
  state, runner, path, _ = setup
  path.unlink()
  if kind == 'fifo':
    os.mkfifo(path)
  elif kind == 'device':
    path.symlink_to('/dev/null')
  elif kind == 'directory':
    path.mkdir()
  with pytest.raises(ValueError, match=str(path)):
    await state.info(str(path), type='sample')
  assert not runner.jobs
  if kind != 'missing':
    path.unlink() if kind != 'directory' else path.rmdir()
  path.write_bytes(b'SAMPLE')
  assert await state.info(str(path))


@pytest.mark.asyncio
async def test_denial_is_the_same_exception_and_releases_admission(setup):
  state, runner, path, _ = setup
  denial = access.FileAccessDenied(path, 'private')

  class Denied(access.AllowAll):
    def open_read(self, path):
      raise denial

  with access.installed(Denied()), pytest.raises(access.FileAccessDenied) as error:
    await state.info(str(path))
  assert error.value is denial
  assert not runner.jobs
  assert await state.info(str(path))


class StreamAccess(access.AllowAll):
  def __init__(self, stream):
    self.stream = stream

  def open_read(self, path):
    return self.stream


@pytest.mark.asyncio
async def test_read_error_names_path_and_cause_and_closes_stream(setup):
  state, runner, path, _ = setup

  class Broken(io.BytesIO):
    def read(self, size: int | None = -1):
      raise OSError('test storage failure')

  stream = Broken()
  with (
    access.installed(StreamAccess(stream)),
    pytest.raises(ValueError, match=f'{path}.*test storage failure.*readable regular-file'),
  ):
    await state.info(str(path))
  assert stream.closed
  assert not runner.jobs


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit', [False, True])
async def test_size_cap_reads_only_one_extra_byte_and_releases_slot(setup, explicit):
  state, runner, path, file_type = setup
  data = b'SAMPLE' + b'x' * file_type.limits.max_bytes
  stream = io.BytesIO(data)

  class Measured(io.BytesIO):
    def close(self):
      self.consumed = self.tell()
      super().close()

  stream = Measured(data)
  with (
    access.installed(StreamAccess(stream)),
    pytest.raises(
      ValueError,
      match=f'{file_type.limits.max_bytes + 1} bytes.*{file_type.limits.max_bytes}.*smaller',
    ),
  ):
    await state.info(str(path), type='sample' if explicit else None)
  assert stream.consumed == file_type.limits.max_bytes + 1
  assert not runner.jobs
  path.write_bytes(data[: file_type.limits.max_bytes])
  assert (await state.info(str(path))).size_bytes == file_type.limits.max_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit,consumed', [(False, IDENTIFICATION_BYTES), (True, 11)])
async def test_identification_prefix_precedes_low_type_cap(setup, monkeypatch, explicit, consumed):
  state, runner, path, file_type = setup
  small = replace(file_type, limits=replace(file_type.limits, max_bytes=10))
  monkeypatch.setattr(registry, '_entry_points', lambda: (EntryPoint('sample', small),))

  class Measured(io.BytesIO):
    def close(self):
      self.consumed = self.tell()
      super().close()

  stream = Measured(b'SAMPLE' + b'x' * 10000)
  with access.installed(StreamAccess(stream)), pytest.raises(ValueError, match='limit of 10 bytes'):
    await state.info(str(path), type='sample' if explicit else None)
  assert stream.consumed == consumed
  assert not runner.jobs


@pytest.mark.asyncio
async def test_short_reads_still_identify_full_prefix_and_hash_every_byte(setup, monkeypatch):
  state, _, path, file_type = setup
  late = replace(file_type, signatures=(Signature(b'SAMPLE', within=4090),))
  monkeypatch.setattr(registry, '_entry_points', lambda: (EntryPoint('sample', late),))
  data = b'x' * 4090 + b'SAMPLE' + b'y' * 200

  class Short(io.BytesIO):
    def read(self, size: int | None = -1):
      return super().read(min(size, 7) if size is not None else 7)

  with access.installed(StreamAccess(Short(data))):
    assert (await state.info(str(path))).sha256 == hashlib.sha256(data).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'view,pages,reason',
  [
    ('absent', None, 'unknown view'),
    ('whole', '1', 'omit pages'),
    ('text', '9', 'narrow pages'),
    ('text', '0', 'positive page numbers'),
  ],
)
async def test_bad_view_and_pages_name_cause_and_fix_without_worker(setup, view, pages, reason):
  state, runner, path, _ = setup
  with pytest.raises(ValueError, match=reason):
    await state.view(str(path), view, pages)
  assert not runner.jobs
  assert await state.info(str(path))


@contextmanager
def held_read(loop, *, fail=False):
  entered = asyncio.Event()
  finished = asyncio.Event()
  release = threading.Event()

  class Held(io.BytesIO):
    def read(self, size: int | None = -1):
      loop.call_soon_threadsafe(entered.set)
      release.wait()
      if fail:
        raise OSError('late failure')
      return super().read(size)

    def close(self):
      super().close()
      loop.call_soon_threadsafe(finished.set)

  stream = Held(b'SAMPLE')
  try:
    yield stream, entered, release, finished
  finally:
    release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize('abandon', ['cancel', 'timeout'])
@pytest.mark.parametrize('late_failure', [False, True])
async def test_abandoned_read_retains_slot_but_not_event_loop(setup, abandon, late_failure):
  _, runner, path, _ = setup
  slots = WorkerSlots(budget_bytes=2 * SandboxLimits().address_space_bytes, queue_seconds=0.01)
  state = Operations(runner, slots=slots, read_seconds=0.03 if abandon == 'timeout' else 10)
  loop = asyncio.get_running_loop()
  with held_read(loop, fail=late_failure) as (stream, entered, release, finished):
    with access.installed(StreamAccess(stream)):
      call = asyncio.create_task(state.info(str(path)))
    await entered.wait()
    if abandon == 'cancel':
      call.cancel()
      with pytest.raises(asyncio.CancelledError):
        await call
    else:
      with pytest.raises(RuntimeError, match='read timed out'):
        await call
    assert not stream.closed
    assert await state.info(str(path))
    with await slots.acquire():
      with pytest.raises(RuntimeError, match='every slot is busy'):
        await state.info(str(path))
    release.set()
    await finished.wait()
    with await slots.acquire():
      assert await state.info(str(path))


@pytest.mark.asyncio
async def test_timeout_budget_starts_at_admission_including_open(setup):
  _, runner, path, _ = setup
  slots = WorkerSlots(budget_bytes=SandboxLimits().address_space_bytes)
  state = Operations(runner, slots=slots, read_seconds=0.02)
  entered = asyncio.Event()
  release = threading.Event()
  loop = asyncio.get_running_loop()

  class SlowOpen(access.AllowAll):
    def open_read(self, path):
      loop.call_soon_threadsafe(entered.set)
      release.wait()
      return super().open_read(path)

  with ExitStack() as stack:
    stack.callback(release.set)
    with access.installed(SlowOpen()):
      call = asyncio.create_task(state.info(str(path)))
    await entered.wait()
    with pytest.raises(RuntimeError, match='read timed out'):
      await call
    release.set()
    with await slots.acquire():
      pass


@pytest.mark.asyncio
async def test_thread_start_failure_does_not_leak_slot(setup, monkeypatch):
  _, runner, path, _ = setup
  slots = WorkerSlots(budget_bytes=SandboxLimits().address_space_bytes, queue_seconds=0.01)
  state = Operations(runner, slots=slots)

  def fail(thread):
    raise RuntimeError('thread unavailable')

  with monkeypatch.context() as patch:
    patch.setattr(threading.Thread, 'start', fail)
    with pytest.raises(RuntimeError, match='thread unavailable'):
      await state.info(str(path))
  assert await state.info(str(path))


@pytest.mark.asyncio
async def test_module_library_functions_use_shared_operations(setup, monkeypatch):
  state, runner, path, _ = setup
  monkeypatch.setattr(operations, '_default', state)
  assert await operations.info(str(path))
  assert await operations.view(str(path), 'text', '1')
  assert len(runner.jobs) == 2


def test_state_construction_and_close_need_no_running_loop():
  state = Operations()
  state.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['unknown', 'unidentified', 'ambiguous', 'none', 'broken'])
async def test_type_failures_name_cause_and_remedy_and_release_slot(setup, monkeypatch, fault):
  state, runner, path, file_type = setup
  explicit = None
  exception = ValueError
  if fault == 'unknown':
    explicit, reason = 'absent', 'unknown file type.*installed file types: sample'
  elif fault == 'unidentified':
    path.write_bytes(b'no signature')
    reason = 'no file type matches.*pass type explicitly'
  elif fault == 'ambiguous':
    monkeypatch.setattr(
      registry,
      '_entry_points',
      lambda: (
        EntryPoint('sample', file_type),
        EntryPoint('second', replace(file_type, name='second')),
      ),
    )
    reason = 'ambiguous file type.*pass type explicitly'
  elif fault == 'none':
    monkeypatch.setattr(registry, '_entry_points', lambda: ())
    reason = 'no file types installed; install'
  else:
    explicit = 'sample'
    monkeypatch.setattr(
      registry, '_entry_points', lambda: (EntryPoint('sample', ImportError('dependency missing')),)
    )
    exception, reason = RuntimeError, "failed to load file type 'sample'.*dependency missing"
  with pytest.raises(exception, match=reason):
    await state.info(str(path), type=explicit)
  assert not runner.jobs
  monkeypatch.setattr(registry, '_entry_points', lambda: (EntryPoint('sample', file_type),))
  path.write_bytes(b'SAMPLE')
  assert await state.info(str(path))


@pytest.mark.asyncio
async def test_explicit_type_ignores_extension_and_signature(setup):
  state, _, path, _ = setup
  path.write_bytes(b'no signature')
  assert await state.info(str(path), type='sample')


@pytest.mark.asyncio
async def test_admission_precedes_open_and_lasts_through_worker(setup):
  state, runner, path, _ = setup
  started = asyncio.Event()
  release = asyncio.Event()
  original = runner.run

  async def held(job):
    started.set()
    await release.wait()
    return await original(job)

  runner.run = held
  call = asyncio.create_task(state.info(str(path)))
  await started.wait()
  with pytest.raises(RuntimeError, match='every slot is busy'):
    await state.info(str(path.parent / 'missing'))
  call.cancel()
  with pytest.raises(asyncio.CancelledError):
    await call
  runner.run = original
  assert await state.info(str(path))
