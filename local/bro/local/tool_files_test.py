import asyncio
import hashlib
import json
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Annotated, ClassVar

import pytest
from pydantic import Field
from starlette.testclient import TestClient

from bro import artifact, artifact_mcp, results
from bro.llm.mcp import ToolRegistry, encode_tool_result
from bro.mcp import Toolset, mount
from bro.runtime.mcp_server import create_http_app
from bro.tool_files import (
  FILE_PARAMETER_DESCRIPTION,
  RETURNED_FILE_GUIDANCE,
  ArtifactFile,
  return_bytes,
  take,
)
from bro.workspace.paths import MINT_DIR_ENV
from bros.bro import Bro

_toolset = Toolset[None]('documents')


@_toolset.tool('return a generated report. ' + RETURNED_FILE_GUIDANCE)
def report() -> ArtifactFile:
  return return_bytes(b'generated report', 'report.txt')


@_toolset.tool('read a file parameter')
def consume(ref: Annotated[str, Field(description=FILE_PARAMETER_DESCRIPTION)]) -> str:
  return Path(take(ref)['path']).read_text()


class _FileBro(Bro):
  name = 'tool-file-test'
  tools: ClassVar = [mount(artifact_mcp.toolset), mount(_toolset)]


@pytest.mark.parametrize('harness', ['bro', 'claude'])
def test_file_tools_through_both_assembled_harness_routes(harness, tmp_path, monkeypatch):
  mint_directory = tmp_path / 'mint'
  mint_directory.mkdir()
  tree = tmp_path / 'tree'
  tree.mkdir()
  (tree / 'input.txt').write_text('file from the tree')
  view = tmp_path / 'view'
  monkeypatch.chdir(tree)
  monkeypatch.setenv(MINT_DIR_ENV, str(mint_directory))
  store = results.Store(tmp_path / 'results')
  monkeypatch.setattr(results, 'session_store', lambda: store)
  objects = {}
  requests = []

  def request(kind, arguments, timeout):
    requests.append((kind, arguments))
    if kind == artifact.MINT:
      if 'entry' in arguments:
        path = mint_directory / arguments['entry']
      else:
        path = Path(arguments['path'])
        if not path.is_relative_to(tree):
          raise artifact.ArtifactError('path is outside the tree')
      data = path.read_bytes()
      digest = 'sha256:' + hashlib.sha256(data).hexdigest()
      objects[digest] = data
      return {'ref': artifact.compose_ref(digest, arguments['name']), 'size': len(data)}
    assert kind == artifact.FETCH
    assert arguments['checked'] is True
    digest, _ = artifact.split_ref(arguments['ref'])
    path = view / arguments['ref']
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(objects[digest])
    return {'path': str(path)}

  monkeypatch.setattr(artifact, '_call', request)
  with ExitStack() as stack:
    bro = stack.enter_context(closing(_FileBro()))
    assert not bro.reach().files
    assert bro.reach().brash is None
    servers = bro.assemble(harness=harness, hold='unattended')
    registry = ToolRegistry(servers)
    tools = {tool.name: tool for tool in asyncio.run(registry.resolve())}
    assert RETURNED_FILE_GUIDANCE in tools['documents__report'].description
    assert all(
      field in tools['documents__report'].description for field in ('ref:', 'path:', 'size:')
    )
    assert (
      tools['documents__consume'].parameters['properties']['ref']['description']
      == FILE_PARAMETER_DESCRIPTION
    )
    client = (
      stack.enter_context(TestClient(create_http_app(servers, 'test-token')))
      if harness == 'claude'
      else None
    )

    def call(namespace, name, arguments):
      if client is None:
        return encode_tool_result(asyncio.run(registry.call(f'{namespace}__{name}', arguments)))
      response = client.post(
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
      result = response.json()['result']
      assert not result.get('isError', False), result
      return result['content'][0]['text']

    minted = json.loads(call('artifact', 'mint', {'path': 'input.txt'}))
    assert minted['size'] == len(b'file from the tree')
    assert call('documents', 'consume', {'ref': minted['ref']}) == 'file from the tree'
    renamed = json.loads(call('artifact', 'mint', {'path': 'input.txt', 'name': 'named.txt'}))
    assert renamed['ref'] == minted['ref'].removesuffix('input.txt') + 'named.txt'
    returned = json.loads(call('documents', 'report', {}))
    assert Path(returned['path']).read_bytes() == b'generated report'
    assert returned['size'] == len(b'generated report')
    assert call('documents', 'consume', {'ref': returned['ref']}) == 'generated report'
    assert list(mint_directory.iterdir()) == []
    assert requests[0] == (artifact.MINT, {'path': str(tree / 'input.txt'), 'name': 'input.txt'})
