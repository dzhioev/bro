#!/usr/bin/env python
"""Checkout benchmark for confined PDF tools; one JSON record per measurement."""

import asyncio
import hashlib
import json
import platform
import resource
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from bro.base.args import Parser
from bro.files.operations import Operations
from bro.llm.mcp import encode_tool_result
from bro.pdf.attachments import TEXT_BYTES
from bro.pdf.page_text import HIDDEN_EXCERPT_CHARACTERS, HIDDEN_NOTES
from bro.pdf.pdf_test_helper import (
  attachments_view_cases,
  fields_view_cases,
  links_view_cases,
  make_pdf,
  text_operators,
  text_view_cases,
)

__cli_name__ = 'file-bench'
ROOT = Path(__file__).resolve().parents[3]
CELLS = ('speed', 'tokens', 'security', 'fidelity')


@dataclass(frozen=True)
class PublicInput:
  url: str
  sha256: str
  pages: int


PUBLIC_INPUTS = {
  'paper': PublicInput(
    'https://arxiv.org/pdf/1706.03762v7',
    'bdfaa68d8984f0dc02beaca527b76f207d99b666d31d1da728ee0728182df697',
    15,
  ),
  'tax': PublicInput(
    'https://www.irs.gov/pub/irs-prior/f1040--2023.pdf',
    'a410ce39e1f9b75ab3ddd8c314b9e7dce022ed6e3aebde5418a58b88f1e0213c',
    2,
  ),
}


class Report:
  def __init__(self):
    self.failed = False

  def record(self, cell: str, name: str, status: str = 'ok', **values: Any) -> None:
    self.failed |= status == 'failed'
    print(json.dumps({'cell': cell, 'name': name, 'status': status, **values}), flush=True)

  def gate(self, name: str, measured: float, maximum: float, unit: str) -> None:
    self.record(
      'speed',
      name,
      'ok' if measured <= maximum else 'failed',
      measured=measured,
      maximum=maximum,
      unit=unit,
    )


def public_input(name: str, cache: Path) -> Path | None:
  source = PUBLIC_INPUTS[name]
  path = cache / f'{name}.pdf'
  if path.exists():
    data = path.read_bytes()
  else:
    try:
      with urllib.request.urlopen(source.url, timeout=30) as response:
        data = response.read()
    except urllib.error.HTTPError:
      raise
    except (urllib.error.URLError, TimeoutError, ConnectionError):
      return None
  if hashlib.sha256(data).hexdigest() != source.sha256:
    raise ValueError(f'SHA-256 mismatch for {source.url}')
  cache.mkdir(parents=True, exist_ok=True)
  path.write_bytes(data)
  return path


def dense_page() -> tuple[bytes, str]:
  lines = [
    f'{index:02d} Invoice item 1234567890 amount 1234.56 reference ABCDE' for index in range(50)
  ]
  return b''.join(
    text_operators(line, size=9, y=760 - index * 14) for index, line in enumerate(lines)
  ), '\n'.join(lines)


def cpu_seconds() -> float:
  return sum(
    usage.ru_utime + usage.ru_stime
    for usage in (
      resource.getrusage(resource.RUSAGE_SELF),
      resource.getrusage(resource.RUSAGE_CHILDREN),
    )
  )


async def measure(path: Path, view: str | None) -> tuple[dict[str, Any], float, float]:
  started = time.perf_counter()
  cpu = cpu_seconds()
  with closing(Operations()) as operations:
    result = (
      await operations.info(str(path)) if view is None else await operations.view(str(path), view)
    )
  return result.model_dump(), time.perf_counter() - started, cpu_seconds() - cpu


async def speed(directory: Path, public: dict[str, Path], report: Report) -> None:
  content, expected = dense_page()
  dense = directory / 'dense.pdf'
  dense.write_bytes(make_pdf([content] * 330))
  result, wall, cpu = await measure(dense, 'text')
  pages = result['content']['pages']
  report.record(
    'speed',
    'dense text',
    'ok'
    if len(pages) == 330 and all(page['text'] == expected and not page['notes'] for page in pages)
    else 'failed',
    wall_seconds=wall,
    cpu_seconds=cpu,
    characters=sum(len(page['text']) for page in pages),
  )
  report.gate('dense text CPU', cpu, 15, 'CPU seconds (parent + reaped workers)')
  large = directory / '5000-pages.pdf'
  large.write_bytes(make_pdf([content, content, '   ', b'0 0 10 10 re f', None] * 1000))
  result, wall, cpu = await measure(large, None)
  details = result['details']
  report.record(
    'speed',
    '5000-page info',
    'ok'
    if details['page_count'] == 5000 and details['without_text_page_count'] == 3000
    else 'failed',
    wall_seconds=wall,
    cpu_seconds=cpu,
  )
  report.gate('5000-page info wall', wall, 10, 'wall seconds')
  small = directory / 'invoice.pdf'
  small.write_bytes(
    make_pdf(
      ['Invoice 42'],
      fields=[{'name': 'total', 'value': '1234.56'}],
      links=[{}],
      attachments=[('invoice.xml', b'<invoice/>', None)],
    )
  )
  for view in (None, 'text', 'links', 'attachments', 'fields'):
    _, wall, cpu = await measure(small, view)
    report.record('speed', f'invoice {view or "info"}', wall_seconds=wall, cpu_seconds=cpu)
  for name, path in public.items():
    for view in (None, 'text'):
      result, wall, cpu = await measure(path, view)
      page_count = (
        result['details']['page_count'] if view is None else len(result['content']['pages'])
      )
      report.record(
        'speed',
        f'{name} {view or "info"}',
        'ok' if page_count == PUBLIC_INPUTS[name].pages else 'failed',
        wall_seconds=wall,
        cpu_seconds=cpu,
        pages=page_count,
      )
      if name == 'paper':
        report.gate(f'paper {view or "info"} wall', wall, 1, 'wall seconds')


def strings(value: Any) -> list[str]:
  if isinstance(value, str):
    return [value]
  if isinstance(value, list):
    return [text for item in value for text in strings(item)]
  if isinstance(value, dict):
    return [text for item in value.values() for text in strings(item)]
  return []


async def tokens(directory: Path, public: dict[str, Path], report: Report) -> None:
  import tiktoken
  from requests.exceptions import ConnectionError as TokenConnectionError, Timeout

  try:
    encoding = tiktoken.get_encoding('o200k_base')
  except (TokenConnectionError, Timeout) as error:
    report.record('tokens', 'o200k_base', 'skipped', reason=str(error))
    return
  inputs = dict(public)
  samples = {
    'Latin': 'Invoice number 42. Total 1234.56 euros.',
    'Cyrillic': 'Номер счёта 42. Итого 1234.56 рублей.',
  }
  for name, text in samples.items():
    mapping = {
      128 + index: character
      for index, character in enumerate(sorted(set(text) - set(map(chr, range(128)))))
    }
    reverse = {character: chr(code) for code, character in mapping.items()}
    path = directory / f'{name}.pdf'
    path.write_bytes(
      make_pdf(
        [''.join(reverse.get(character, character) for character in text)],
        character_map=mapping,
        links=[{'uri': b'https://example.test/invoice/42'}],
        attachments=[('invoice.txt', text.encode(), 'text/plain')],
        fields=[{'name': 'invoice', 'value': text}],
      )
    )
    inputs[name] = path
  for name, path in inputs.items():
    pages = PUBLIC_INPUTS[name].pages if name in PUBLIC_INPUTS else 1
    for view in ('text', 'links', 'attachments', 'fields'):
      result, _, _ = await measure(path, view)
      encoded = encode_tool_result(result)
      count = len(encoding.encode(encoded, disallowed_special=()))
      plain = sum(
        len(encoding.encode(text, disallowed_special=())) for text in strings(result['content'])
      )
      report.record(
        'tokens',
        f'{name} {view}',
        pages=pages,
        tokens=count,
        tokens_per_page=count / pages,
        content_string_tokens=plain,
        envelope_and_json_tokens=count - plain,
      )


async def fidelity(directory: Path, report: Report) -> None:
  path = directory / 'fidelity.pdf'
  with closing(Operations()) as operations:
    for name, arguments, text, notes in text_view_cases(
      note_limit=HIDDEN_NOTES, excerpt_characters=HIDDEN_EXCERPT_CHARACTERS
    ):
      path.write_bytes(make_pdf(**arguments))
      result = await operations.view(str(path), 'text')
      report.record(
        'fidelity',
        f'text {name}',
        'ok'
        if result.content == {'pages': [{'page_number': 1, 'notes': notes, 'text': text}]}
        else 'failed',
      )
    for view, cases in (('links', links_view_cases()), ('fields', fields_view_cases())):
      for name, arguments, expected in cases:
        path.write_bytes(make_pdf(**arguments))
        result = await operations.view(str(path), view)
        report.record(
          'fidelity', f'{view} {name}', 'ok' if result.content == {view: expected} else 'failed'
        )
    for name, entries, expected in attachments_view_cases(TEXT_BYTES):
      path.write_bytes(make_pdf(attachments=entries))
      result = await operations.view(str(path), 'attachments')
      report.record(
        'fidelity',
        f'attachments {name}',
        'ok'
        if result.content == {'attachment_count': len(entries), 'attachments': expected}
        else 'failed',
      )


def security(report: Report) -> None:
  result = subprocess.run(
    [
      sys.executable,
      '-m',
      'pytest',
      '-q',
      '-n',
      'auto',
      '--maxprocesses=8',
      'bro/files/confinement_sandbox_test.py',
    ],
    cwd=ROOT,
    capture_output=True,
    text=True,
  )
  report.record(
    'security',
    'confinement',
    'ok' if result.returncode == 0 else 'failed',
    architecture=platform.machine(),
    output=result.stdout + result.stderr,
  )
  # The corpus stage adds its command here; it must contribute to Report.failed.
  report.record(
    'security', 'hostile corpus', 'deferred', task='https://github.com/dzhioev/bro/issues/1086'
  )


async def benchmark(cells: list[str], offline: bool, cache: Path) -> bool:
  report = Report()
  public = {}
  if set(cells) & {'speed', 'tokens'}:
    for name in PUBLIC_INPUTS:
      path = None if offline else public_input(name, cache)
      if path is None:
        report.record(
          'inputs', name, 'skipped', reason='offline' if offline else 'network unavailable'
        )
      else:
        public[name] = path
  with TemporaryDirectory(prefix='file-bench-') as temporary:
    directory = Path(temporary)
    for cell in cells:
      if cell == 'speed':
        await speed(directory, public, report)
      elif cell == 'tokens':
        if offline:
          report.record('tokens', 'o200k_base', 'skipped', reason='offline')
        else:
          await tokens(directory, public, report)
      elif cell == 'fidelity':
        await fidelity(directory, report)
      elif cell == 'security':
        security(report)
      else:
        raise ValueError(f'unknown benchmark cell: {cell}')
  return not report.failed


def main(argv: list[str]) -> None:
  parser = Parser(description=__doc__)
  parser.add_argument(
    '--cell', action='append', choices=CELLS, help='run only this cell; repeatable, defaults to all'
  )
  parser.add_argument(
    '--offline', action='store_true', help='skip public inputs and tokens, even when cached'
  )
  parser.add_argument(
    '--cache',
    type=Path,
    default=Path.home() / '.cache/bro/file-bench',
    help='SHA-256-verified public input cache',
  )
  arguments = parser.parse(argv)
  if not asyncio.run(
    benchmark(arguments['cell'] or list(CELLS), arguments['offline'], arguments['cache'])
  ):
    raise SystemExit(1)


if __name__ == '__main__':
  main(sys.argv)
