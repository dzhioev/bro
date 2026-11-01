"""Opt-in file-input probes; raw vendor responses are saved under pytest's temporary path."""

import json
import struct
import zlib

import httpx2
import pytest
from pydantic import BaseModel

from bro.base import credentials
from bro.base.suite_environment import host_credential_store
from bro.deepseek.declaration import Recipe as ChatRecipe
from bro.llm import providers, wires
from bro.llm.wire import File, Text
from bro.openai.declaration import MODELS, Recipe as ResponsesRecipe


class Answer(BaseModel):
  marker: str


def pdf() -> bytes:
  stream = b'BT /F1 24 Tf 50 100 Td (MODEL INPUT PROBE) Tj ET'
  objects = [
    b'<< /Type /Catalog /Pages 2 0 R >>',
    b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream',
  ]
  data = b'%PDF-1.4\n'
  offsets = []
  for index, value in enumerate(objects, 1):
    offsets.append(len(data))
    data += f'{index} 0 obj\n'.encode() + value + b'\nendobj\n'
  start = len(data)
  data += f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode()
  data += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets)
  return (
    data
    + f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode()
  )


def png() -> bytes:
  def chunk(kind, data):
    return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))

  return (
    b'\x89PNG\r\n\x1a\n'
    + chunk(b'IHDR', struct.pack('!2I5B', 32, 32, 8, 2, 0, 0, 0))
    + chunk(b'IDAT', zlib.compress((b'\x00' + b'\xff\x00\x00' * 32) * 32))
    + chunk(b'IEND', b'')
  )


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['pdf-named', 'pdf-unnamed', 'png-responses', 'png-chat'])
async def test_model_file_inputs_live(case, tmp_path, monkeypatch):
  with host_credential_store():
    if case == 'png-chat':
      recipe = ChatRecipe(model='deepseek-flash', reasoning_effort='none')
      if (
        credentials.available('mu')
        and providers.parse(credentials.get_json('mu')['llm']).provider == recipe.TYPE
      ):
        secret = credentials.get_json('mu')['credential']
      elif credentials.available('deepseek'):
        secret = credentials.get_json('deepseek')
      else:
        pytest.skip('needs deepseek or a mu credential selecting deepseek (mu+deepseek)')
    else:
      recipe = ResponsesRecipe(model=MODELS['terra'], reasoning_effort='low')
      if not credentials.available('openai'):
        pytest.skip('needs the openai credential')
      secret = credentials.get_json('openai')
  send = httpx2.AsyncClient.send

  async def capture(client, request, **kwargs):
    response = await send(client, request, **kwargs)
    await response.aread()
    (tmp_path / f'{case}-response.json').write_text(
      json.dumps({'status': response.status_code, 'body': response.json()}, indent=2)
    )
    return response

  monkeypatch.setattr(httpx2.AsyncClient, 'send', capture)
  attachment = (
    File(pdf(), 'application/pdf', 'probe.pdf' if case == 'pdf-named' else None)
    if case.startswith('pdf-')
    else File(png(), 'image/png', 'probe.png')
  )
  result = await wires.open(recipe, secret).structured(
    'Read the attached file. Set marker to MODEL INPUT PROBE for a document, or RED for a red image.',
    Answer,
    (Text('Return the marker.'), attachment),
    120,
  )
  assert result.marker == ('MODEL INPUT PROBE' if case.startswith('pdf-') else 'RED')
