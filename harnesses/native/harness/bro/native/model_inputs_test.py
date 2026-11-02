import json
import threading
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import BaseModel

from bro.base import credentials
from bro.deepseek.declaration import Recipe as ChatRecipe
from bro.llm.llm import UnsupportedContent
from bro.llm.mu import mu
from bro.llm.wire import File, Text
from bro.native.llm import main
from bro.native.wire_fixtures_test_helper import message, response
from bro.openai.declaration import Recipe as ResponsesRecipe
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
  configure(monkeypatch, 'openai:gpt-5.6-terra,reasoning_effort=low')
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
  'recipe,model',
  [('deepseek:deepseek-v4-pro', 'deepseek-v4-pro'), ('deepseek:deepseek-flash', 'deepseek-flash')],
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
    {
      **ChatRecipe.MODEL_INPUTS,
      ChatRecipe(model='deepseek-v4-pro').model: frozenset({'application/pdf'}),
    },
  )
  configure(monkeypatch, 'deepseek:deepseek-v4-pro')
  with pytest.raises(UnsupportedContent, match='Chat cannot carry application/pdf'):
    await mu.aio('Extract totals.', Totals, File(b'pdf', 'application/pdf'))
  assert endpoint == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'filename,encoded', [(None, 'file'), ('scans/march.pdf', 'scans/march.pdf')]
)
async def test_mu_preserves_filename_or_sends_placeholder(endpoint, monkeypatch, filename, encoded):
  configure(monkeypatch, 'openai:gpt-5.6-terra,reasoning_effort=low')
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
  configure(monkeypatch, 'openai:gpt-5.6-terra,reasoning_effort=low')
  with pytest.raises(ValueError, match='must not be empty'):
    mu('Extract totals.', Totals)
  assert endpoint == []


@pytest.mark.parametrize('model', ['deepseek-flash', 'deepseek-v4-pro'])
def test_diagnostic_command_attachment_route(endpoint, monkeypatch, tmp_path, model):
  configure(monkeypatch, 'deepseek:deepseek-flash')
  path = tmp_path / 'chart.png'
  path.write_bytes(b'png')
  arguments = [
    'llm',
    '--llm',
    f'deepseek:{model}',
    'Describe this.',
    '--attach',
    str(path),
  ]
  if model == 'deepseek-v4-pro':
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


@pytest.mark.asyncio
@pytest.mark.parametrize('passthrough', [False, True])
async def test_mu_tiff_passes_the_model_gate_only_with_passthrough(
  endpoint, monkeypatch, passthrough
):
  configure(monkeypatch, 'openai:gpt-6.1-sol' + ('+passthrough' if passthrough else ''))
  content = File(b'tiff fixture', 'image/tiff', 'scan.tiff')
  if not passthrough:
    with pytest.raises(UnsupportedContent, match='image/tiff'):
      await mu.aio('Extract totals.', Totals, content)
    assert endpoint == []
  else:
    assert await mu.aio('Extract totals.', Totals, content) == Totals(total=42)
    assert endpoint[0][1]['input'][1]['content'] == [
      {'type': 'input_image', 'image_url': content.data_url(), 'detail': 'auto'}
    ]


@pytest.mark.asyncio
async def test_mu_unlisted_material_requires_passthrough_before_opening_wire(endpoint, monkeypatch):
  from bro.llm.providers import RecipeError

  configure(monkeypatch, 'openai:gpt-7,reasoning_effort=low')
  with pytest.raises(ValueError, match='mu credential:.*gpt-7.*passthrough') as caught:
    await mu.aio('Extract totals.', Totals, Text('Read this.'))
  assert endpoint == []
  assert isinstance(caught.value.__cause__, RecipeError)
  configure(monkeypatch, 'openai:gpt-7+passthrough,reasoning_effort=low')
  assert await mu.aio('Extract totals.', Totals, Text('Read this.')) == Totals(total=42)
  assert endpoint[0][1]['model'] == 'gpt-7'


def test_diagnostic_command_refuses_unlisted_models(endpoint, monkeypatch):
  from bro.llm.providers import RecipeError

  configure(monkeypatch, 'openai:gpt-6.1-sol')
  with pytest.raises(RecipeError, match='gpt-7.*passthrough'):
    main(['llm', '--llm', 'openai:gpt-7', 'Read this.'])
  assert endpoint == []


@pytest.mark.parametrize('explicit', [False, True])
def test_chat_fork_preserves_the_recorded_recipe_unless_explicitly_replaced(
  endpoint, monkeypatch, tmp_path, register_test_bros, explicit
):
  import asyncio
  from contextlib import nullcontext

  from bro.launch.call import chat_main
  from bro.llm.resolved import ResolvedRecipe
  from bro.native import runner as native_runner
  from bro.native.runner import Runner
  from bro.registry import create_bro
  from bro.trails.local import LocalStore
  from bro.trails.record.bro import Recorder
  from bros.bro import Bro

  class Reader(Bro):
    name = 'admission-reader'

  register_test_bros(Reader)
  from bro.base import host_config

  (tmp_path / 'bro-config.json').write_text(
    json.dumps({'defaults': {'llm': {'bro': 'openai:gpt-5.6-terra'}}})
  )
  monkeypatch.setattr(host_config, 'HOST_CONFIG_FILE', tmp_path / 'bro-config.json')
  configure(monkeypatch, 'openai:gpt-6.1-sol')
  store = LocalStore(tmp_path / 'trails')
  monkeypatch.setattr(native_runner, '_default_tracker_factory', lambda: Recorder(store))
  monkeypatch.setattr('bro.trails.store.default_store', lambda: store)
  monkeypatch.setattr('bro.launch.broxy.session_broxy', nullcontext)
  recorded_runner = Runner(
    create_bro(Reader.name), ResponsesRecipe(model='gpt-7', passthrough=True)
  )
  asyncio.run(recorded_runner.run('start', surface='test', hold='guided'))
  assert recorded_runner.trail_id is not None

  expected = (
    ResolvedRecipe(ResponsesRecipe(model='gpt-5.6-terra'), '--llm')
    if explicit
    else ResolvedRecipe(recorded_runner.recipe, f'trail {recorded_runner.trail_id}')
  )

  async def continue_chat(runner, initial, history=None, hold='guided', preset_name=None):
    assert runner.resolved_recipe == expected
    await runner.send('continue', surface='test', hold=hold)

  monkeypatch.setattr('bro.launch.call.call_text', continue_chat)
  monkeypatch.setattr('bro.launch.call._tui_supported', lambda: False)
  assert (
    chat_main(
      [
        'bro',
        Reader.name,
        '--fork',
        recorded_runner.trail_id,
        *(['--llm', 'openai:gpt-5.6-terra'] if explicit else []),
      ],
      program=['bro', 'chat'],
    )
    is None
  )
  assert endpoint[-1][1]['model'] == expected.recipe.model
  assert endpoint[0][1]['model'] == recorded_runner.recipe.model
