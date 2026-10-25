import hashlib
import json
import re
from contextlib import closing

import pytest
from starlette.testclient import TestClient

from bro import results
from bro.files import operations
from bro.files.slots import WorkerSlots
from bro.llm.mcp import ToolRegistry, encode_tool_result
from bro.local.files_test_helper import (
  ATTACHMENT,
  FIELD,
  HIDDEN,
  INVOICE_NUMBER,
  LINK,
  SUPPLIER,
  TOTAL,
  invoice,
)
from bro.pdf.pdf_test_helper import make_pdf
from bro.runtime.mcp_server import create_http_app
from bros.bro_file_probe import BroFileProbe


class NativeRoute:
  def __init__(self, servers):
    self.tools = ToolRegistry(servers)

  async def call(self, namespace, name, arguments):
    return encode_tool_result(await self.tools.call(f'{namespace}__{name}', arguments))

  async def failure(self, arguments, cause, exception=ValueError):
    with pytest.raises(exception, match=cause):
      await self.call('file', 'view', arguments)


class ClaudeRoute:
  def __init__(self, client):
    self.client = client

  def request(self, namespace, name, arguments):
    response = self.client.post(
      f'/{namespace}',
      headers={
        'Authorization': 'Bearer test-token',
        'Accept': 'application/json, text/event-stream',
      },
      json={
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'tools/call',
        'params': {'name': name, 'arguments': arguments},
      },
    )
    assert response.status_code == 200, response.text
    return response.json()['result']

  async def call(self, namespace, name, arguments):
    result = self.request(namespace, name, arguments)
    assert not result.get('isError', False), result
    assert (
      'structuredContent' not in result
      or json.loads(result['content'][0]['text']) == result['structuredContent']
    )
    return result['content'][0]['text']

  async def failure(self, arguments, cause, exception=ValueError):
    result = self.request('file', 'view', arguments)
    assert result['isError'] is True
    assert re.search(cause, result['content'][0]['text'])


@pytest.fixture
def worker_slots(request, monkeypatch):
  slots = WorkerSlots(budget_bytes=getattr(request, 'param', None))
  monkeypatch.setattr(operations, 'default_slots', slots)
  return slots


@pytest.fixture(params=['bro', 'claude'])
def route(request, monkeypatch, tmp_path, worker_slots):
  store = results.Store(tmp_path / 'results')
  monkeypatch.setattr(results, 'session_store', lambda: store)
  with closing(BroFileProbe()) as bro:
    servers = bro.assemble(harness=request.param, hold='unattended')
    if request.param == 'bro':
      yield NativeRoute(servers)
    else:
      with TestClient(create_http_app(servers, 'test-token')) as client:
        yield ClaudeRoute(client)


@pytest.mark.asyncio
async def test_invoice_through_assembled_serving_routes(route, tmp_path):
  data = invoice()
  path = tmp_path / 'invoice.not-pdf'
  path.write_bytes(data)
  info = json.loads(await route.call('file', 'info', {'path': str(path)}))
  assert info['type'] == 'pdf'
  assert info['sha256'] == hashlib.sha256(data).hexdigest()
  assert info['size_bytes'] == len(data)
  assert info['details']['page_count'] == 1
  assert info['details']['title'] == f'Invoice {SUPPLIER}'
  for view in ('text', 'links', 'attachments', 'fields'):
    text = await route.call('file', 'view', {'path': str(path), 'view': view})
    value = json.loads(text)
    assert text == json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    assert value['sha256'] == info['sha256']
    content = value['content']
    if view == 'text':
      page = content['pages'][0]
      assert (
        page['text']
        == f'Invoice {INVOICE_NUMBER}\nSupplier {SUPPLIER}\nTotal {TOTAL}\nPay invoice\n{HIDDEN}'
      )
      assert page['notes'] == [{'note': 'hidden_text', 'reason': 'invisible', 'text': HIDDEN}]
      assert SUPPLIER in text
    elif view == 'links':
      assert content['links'][0]['uri'] == LINK
      assert content['links'][0]['text'] == 'Pay invoice'
    elif view == 'attachments':
      assert content['attachments'][0]['text'] == ATTACHMENT
      assert ATTACHMENT in text
    else:
      assert content['fields'][0]['name'] == 'purchase_order'
      assert content['fields'][0]['value'] == FIELD


@pytest.mark.asyncio
async def test_long_invoice_pages_kept_result_without_reading_file_again(route, tmp_path):
  path = tmp_path / 'long-invoice'
  path.write_bytes(invoice(long=True))
  first = await route.call('file', 'view', {'path': str(path), 'view': 'text'})
  assert first.index('"notes":') < first.index('"text":')
  assert HIDDEN in first
  assert first.index('"note":"hidden_text"') < first.index('"text":"Invoice')
  path.unlink()
  chunks = []
  reply = first
  offset = 0
  while marker := re.search(
    r'\n\[\.\.\..*bro::page\(result="([^"]+)", char_offset=(\d+)\)\.\.\.\]$', reply
  ):
    chunks.append(reply[: marker.start()])
    next_offset = int(marker[2])
    assert next_offset > offset
    offset = next_offset
    reply = await route.call('bro', 'page', {'result': marker[1], 'char_offset': offset})
  chunks.append(reply)
  assert len(chunks) > 1
  whole = ''.join(chunks)
  assert len(whole) > 30_000
  document = json.loads(whole)
  pages = document['content']['pages']
  assert len(pages) == 13
  assert [page['page_number'] for page in pages] == list(range(1, 14))
  assert SUPPLIER in pages[0]['text']
  assert pages[-1]['text'].endswith('Line 39 ' + 'invoice detail ' * 5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'data,extra,cause',
  [
    (b'%PDF-broken', {}, 'not a readable PDF'),
    (make_pdf(user_password='secret'), {}, 'password'),
    (invoice(), {'pages': '2'}, 'document count'),
    (invoice(), {'view': 'attachments', 'pages': '1'}, 'takes no pages'),
  ],
)
async def test_serving_failures_name_the_cause_and_next_call_succeeds(
  route, tmp_path, data, extra, cause
):
  path = tmp_path / 'document'
  path.write_bytes(data)
  await route.failure({'path': str(path), 'view': 'text', **extra}, cause)
  path.write_bytes(invoice())
  assert json.loads(await route.call('file', 'info', {'path': str(path)}))['type'] == 'pdf'


@pytest.mark.asyncio
@pytest.mark.parametrize('worker_slots', [0], indirect=True)
async def test_serving_environment_failure_names_missing_memory_budget(
  route, tmp_path, worker_slots
):
  path = tmp_path / 'invoice'
  path.write_bytes(invoice())
  await route.failure(
    {'path': str(path), 'view': 'text'},
    'file sandbox unavailable: the memory budget .* cannot hold one worker',
    RuntimeError,
  )
