import json
import threading
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import BaseModel

from bro.base import credentials
from bro.deepseek.declaration import LLMSpec as ChatRecipe
from bro.llm.llm import UnsupportedContent
from bro.llm.mu import mu
from bro.llm.wire import File, Text
from bro.native.llm import main
from bro.native.wire_fixtures_test_helper import message, response
from bro.openai.declaration import LLMSpec as ResponsesRecipe
from bro.wire_chat.fixtures_test_helper import response as chat_response


class Totals(BaseModel):
  total: int


@pytest.fixture
def endpoint(monkeypatch):
  requests = []

  class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
      requests.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
      raw = (
        response(message('{"total":42}'))
        if self.path == '/responses'
        else chat_response('{"total":42}')
      )
      payload = json.dumps(raw).encode()
      self.send_response(200)
      self.send_header('Content-Type', 'application/json')
      self.send_header('Content-Length', str(len(payload)))
      self.end_headers()
      self.wfile.write(payload)

    def log_message(self, format, *args):
      pass

  with ExitStack() as resources:
    server = resources.enter_context(ThreadingHTTPServer(('127.0.0.1', 0), Handler))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    resources.callback(thread.join)
    resources.callback(server.shutdown)
    for recipe in (ResponsesRecipe, ChatRecipe):
      monkeypatch.setattr(recipe, 'ENDPOINT', f'http://127.0.0.1:{server.server_port}')
    yield requests


def configure(monkeypatch, recipe):
  material = {'llm': recipe, 'credential': {'api_key': 'fixture-key'}}
  monkeypatch.setattr(
    credentials, 'get_json', lambda kind: material if kind == 'mu' else material['credential']
  )


@pytest.mark.asyncio
async def test_mu_pdf_route_from_path_returns_validated_result(endpoint, monkeypatch, tmp_path):
  configure(monkeypatch, 'openai:terra:low')
  path = tmp_path / 'march.pdf'
  path.write_bytes(b'pdf fixture')
  result = await mu.aio(
    'Extract the totals.', Totals, Text('Read the report.'), File.from_path(path)
  )
  assert result == Totals(total=42)
  route, request = endpoint[0]
  assert route == '/responses'
  assert request['input'] == [
    {'role': 'system', 'content': 'Extract the totals.'},
    {
      'role': 'user',
      'content': [
        {'type': 'input_text', 'text': 'Read the report.'},
        {
          'type': 'input_file',
          'file_data': 'data:application/pdf;base64,cGRmIGZpeHR1cmU=',
          'filename': 'march.pdf',
          'detail': 'auto',
        },
      ],
    },
  ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'recipe,model', [('deepseek:pro', 'deepseek-v4-pro'), ('deepseek:flash', 'deepseek-flash')]
)
async def test_mu_model_gate_refuses_pdf_without_a_request(endpoint, monkeypatch, recipe, model):
  configure(monkeypatch, recipe)
  with pytest.raises(UnsupportedContent, match=f'application/pdf.*{model}'):
    await mu.aio(
      'Extract totals.', Totals, Text('Read the report.'), File(b'pdf', 'application/pdf')
    )
  assert endpoint == []


@pytest.mark.asyncio
async def test_mu_chat_protocol_gate_refuses_model_accepted_pdf(endpoint, monkeypatch):
  monkeypatch.setattr(
    ChatRecipe,
    'MODEL_INPUTS',
    {**ChatRecipe.MODEL_INPUTS, ChatRecipe().model: frozenset({'application/pdf'})},
  )
  configure(monkeypatch, 'deepseek:pro')
  with pytest.raises(UnsupportedContent, match='Chat cannot carry application/pdf'):
    await mu.aio('Extract totals.', Totals, File(b'pdf', 'application/pdf'))
  assert endpoint == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'filename,encoded', [(None, 'file'), ('scans/march.pdf', 'scans/march.pdf')]
)
async def test_mu_preserves_filename_or_sends_placeholder(endpoint, monkeypatch, filename, encoded):
  configure(monkeypatch, 'openai:terra:low')
  await mu.aio('Extract totals.', Totals, File(b'pdf', 'application/pdf', filename))
  assert endpoint[0][1]['input'][1]['content'] == [
    {
      'type': 'input_file',
      'file_data': 'data:application/pdf;base64,cGRm',
      'filename': encoded,
      'detail': 'auto',
    }
  ]


def test_mu_empty_content_sends_nothing(endpoint, monkeypatch):
  configure(monkeypatch, 'openai:terra:low')
  with pytest.raises(ValueError, match='must not be empty'):
    mu('Extract totals.', Totals)
  assert endpoint == []


@pytest.mark.parametrize('model', ['flash', 'pro'])
def test_diagnostic_command_attachment_route(endpoint, monkeypatch, tmp_path, model):
  configure(monkeypatch, 'deepseek:flash')
  path = tmp_path / 'chart.png'
  path.write_bytes(b'png')
  arguments = [
    'llm',
    '--provider',
    'deepseek',
    '--model',
    model,
    'Describe this.',
    '--attach',
    str(path),
  ]
  if model == 'pro':
    with pytest.raises(UnsupportedContent, match='image/png.*deepseek-v4-pro'):
      main(arguments)
    assert endpoint == []
  else:
    main(arguments)
    assert endpoint[0][0] == '/chat/completions'
    assert endpoint[0][1]['messages'] == [
      {
        'role': 'user',
        'content': [
          {'type': 'text', 'text': 'Describe this.'},
          {
            'type': 'image_url',
            'image_url': {'url': 'data:image/png;base64,cG5n', 'detail': 'auto'},
          },
        ],
      }
    ]
