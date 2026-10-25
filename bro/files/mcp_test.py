import asyncio
import re

import pytest

from bro import results
from bro.files import mcp, operations, registry
from bro.files.operations_test_helper import Content, Details, StubRunner
from bro.files.slots import WorkerSlots
from bro.files.types import Limits, View
from bro.files.types_test_helper import EntryPoint, make_type
from bro.llm.mcp import InProcessMCPServer, ToolRegistry


@pytest.fixture
def served(monkeypatch, tmp_path):
  file_type = make_type(
    tmp_path,
    details=Details,
    views=(View('text', 'Read text', Content, True),),
    limits=Limits(100000, 5, 100000, 20),
  )
  monkeypatch.setattr(registry, '_entry_points', lambda: (EntryPoint('sample', file_type),))
  monkeypatch.setattr(operations, 'registry', registry.TypeRegistry())
  state = operations.Operations(StubRunner(), slots=WorkerSlots(budget_bytes=1024**3))
  monkeypatch.setattr(mcp.toolset, '_state_factory', lambda: state)
  return mcp.toolset.build()


@pytest.mark.asyncio
async def test_tools_adapt_operations_and_read_reference(served, tmp_path):
  path = tmp_path / 'file'
  path.write_bytes(b'SAMPLE')
  tools = ToolRegistry([served])
  info = await tools.call('file__info', {'path': str(path)})
  view = await tools.call('file__view', {'path': str(path), 'view': 'text', 'pages': '2'})
  assert isinstance(info, dict) and isinstance(view, dict)
  assert info['sha256'] == view['sha256']
  assert view['content']['pages'] == [
    {'page_number': 2, 'notes': ['untrusted text'], 'text': 'SAMPLE'}
  ]
  assert 'Installed file types' in await tools.call('file__read_reference', {})
  served.close()
  with pytest.raises(RuntimeError, match='operations are closed'):
    await tools.call('file__info', {'path': str(path)})


@pytest.mark.asyncio
async def test_long_view_uses_kept_windows_with_notes_before_text(served, tmp_path):
  path = tmp_path / 'large'
  document = 'SAMPLE\n' + 'Здравствуйте' * 3000
  path.write_text(document)
  store = results.Store(tmp_path / 'results')
  service = InProcessMCPServer('bro', results.store_tools(lambda: store))
  tools = ToolRegistry(results.kept([served, service], lambda: store))
  reply = await tools.call('file__view', {'path': str(path), 'view': 'text', 'pages': '1'})
  assert isinstance(reply, str)
  assert reply.index('"notes":') < reply.index('"text":')
  assert 'untrusted text' in reply
  marker = re.search(r'\n\[\.\.\..*char_offset=(\d+)\)\.\.\.\]$', reply)
  assert marker is not None
  (entry,) = store.entries()
  remainder = await tools.call('bro__page', {'result': entry.id, 'char_offset': int(marker[1])})
  assert isinstance(remainder, str)
  assert reply[: marker.start()] + remainder == store.read(entry.id)[1]
  assert document.replace('\n', '\\n') in store.read(entry.id)[1]


def test_build_is_lazy_without_loop_or_budget_read(monkeypatch):
  def unexpected():
    raise AssertionError('budget read at build')

  monkeypatch.setattr('bro.files.slots.memory_budget_bytes', unexpected)
  server = mcp.toolset.build()
  assert asyncio.run(server.list_tools())
  server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('first', ['library', 'tool'])
async def test_library_and_mcp_share_the_process_admission_budget(
  served, monkeypatch, tmp_path, first
):
  from bro.files.confinement import SandboxLimits

  budget = WorkerSlots(queue_seconds=0.01)
  monkeypatch.setattr(
    'bro.files.slots.memory_budget_bytes', lambda: SandboxLimits().address_space_bytes
  )
  monkeypatch.setattr(operations, 'default_slots', budget)
  runner = StubRunner()
  original = runner.run
  entered = asyncio.Event()
  release = asyncio.Event()

  async def held(job):
    entered.set()
    await release.wait()
    return await original(job)

  runner.run = held
  monkeypatch.setattr(operations, '_default', operations.Operations(runner))
  monkeypatch.setattr(mcp.toolset, '_state_factory', lambda: operations.Operations(runner))
  tools = ToolRegistry([mcp.toolset.build()])
  path = tmp_path / 'document'
  path.write_bytes(b'SAMPLE')

  async def call(surface):
    if surface == 'library':
      return await operations.info(str(path))
    return await tools.call('file__info', {'path': str(path)})

  task = asyncio.create_task(call(first))
  await entered.wait()
  other = 'tool' if first == 'library' else 'library'
  with pytest.raises(RuntimeError, match='every slot is busy'):
    await call(other)
  task.cancel()
  with pytest.raises(asyncio.CancelledError):
    await task
  runner.run = original
  assert await call(other)
