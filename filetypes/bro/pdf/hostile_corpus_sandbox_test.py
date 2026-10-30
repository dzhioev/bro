"""The hostile PDF corpus, one case per family, through the real confined worker.

Each family's expected outcome is pinned from its first run: a different
outcome fails the case. A case that fails the worker proves a following call
succeeds on the same operations state and that no worker or process-group
member survives.
"""

import os
from collections.abc import Callable
from contextlib import asynccontextmanager, closing
from typing import Any

import pytest

from bro.files.operations import Operations
from bro.files.runner import SubprocessRunner
from bro.pdf.hostile_test_helper import (
  attachment_bomb,
  bomb_beside_real_attachment,
  deep_page_tree,
  flate_content_bomb,
  huge_info_string,
  huge_media_box,
  many_attachments,
  many_pages,
  many_widgets,
  mutants_and_truncations,
  nested_form_xobjects,
  object_stream_bomb,
  oversized_count,
  page_tree_cycle,
  tiny_text_object_bomb,
  whole_page_links,
  xref_prev_loop,
)
from bro.pdf.pdf_test_helper import make_pdf

# A healthy document that must succeed after any hostile family.
_HEALTHY = make_pdf(['still healthy'])


class ObservedRunner(SubprocessRunner):
  """Records every worker leader so the test can prove none survives."""

  def __init__(self):
    super().__init__()
    self.started: list[int] = []

  @asynccontextmanager
  async def _worker(self, job, stdout, stderr):
    async with super()._worker(job, stdout, stderr) as worker:
      self.started.append(worker.process.pid)
      yield worker


def assert_no_surviving_workers(runner: ObservedRunner) -> None:
  for pid in runner.started:
    try:
      os.killpg(pid, 0)
    except ProcessLookupError:
      continue
    raise AssertionError(f'file worker process group {pid} survived')


async def _call(operations: Operations, path: str, view: str | None) -> Any:
  if view is None:
    return await operations.info(path)
  return await operations.view(path, view)


async def _pin(
  tmp_path,
  data: bytes,
  *,
  view: str | None,
  expected: tuple[type[Exception], str] | Callable[[Any], None],
) -> None:
  """Pin one hostile document's outcome, then prove containment and reaping."""
  runner = ObservedRunner()
  with closing(Operations(runner=runner)) as operations:
    path = tmp_path / 'hostile.pdf'
    path.write_bytes(data)
    if isinstance(expected, tuple):
      exception, match = expected
      with pytest.raises(exception, match=match):
        await _call(operations, str(path), view)
    else:
      expected(await _call(operations, str(path), view))
    assert_no_surviving_workers(runner)
    path.write_bytes(_HEALTHY)
    healthy = await operations.view(str(path), 'text')
    assert healthy.content['pages'][0]['text'] == 'still healthy'
    assert_no_surviving_workers(runner)


def _unreadable(result) -> None:
  pages = result.content['pages']
  assert len(pages) == 1
  assert pages[0]['text'] == ''
  assert pages[0]['notes'] == [{'note': 'unreadable'}]


def _text(value: str) -> Callable[[Any], None]:
  def check(result) -> None:
    assert result.content['pages'][0]['text'] == value

  return check


@pytest.mark.asyncio
async def test_flate_content_bomb_is_a_memory_abort(tmp_path):
  await _pin(tmp_path, flate_content_bomb(), view=None, expected=(RuntimeError, 'SIGABRT'))


@pytest.mark.asyncio
async def test_page_tree_cycle_yields_an_unreadable_page(tmp_path):
  await _pin(tmp_path, page_tree_cycle(), view='text', expected=_unreadable)


@pytest.mark.asyncio
async def test_xref_prev_loop_reads_normally(tmp_path):
  await _pin(tmp_path, xref_prev_loop(), view='text', expected=_text('Hello PDF'))


@pytest.mark.asyncio
async def test_deep_page_tree_yields_an_unreadable_page(tmp_path):
  await _pin(tmp_path, deep_page_tree(), view='text', expected=_unreadable)


@pytest.mark.asyncio
async def test_huge_media_box_reads_normally(tmp_path):
  def check(result) -> None:
    page = result.content['pages'][0]
    # The text sits right of the default Letter box, so it is inside only when
    # PDFium kept the genuinely huge MediaBox rather than falling back to it.
    assert page['text'] == 'x'
    assert page['notes'] == []

  await _pin(tmp_path, huge_media_box(), view='text', expected=check)


@pytest.mark.asyncio
async def test_many_pages_stays_within_the_limit(tmp_path):
  def check(result) -> None:
    assert result.details['page_count'] == 5000

  await _pin(tmp_path, many_pages(), view=None, expected=check)


@pytest.mark.asyncio
async def test_oversized_count_is_rejected(tmp_path):
  await _pin(tmp_path, oversized_count(), view=None, expected=(ValueError, 'above the limit'))


@pytest.mark.asyncio
async def test_object_stream_bomb_is_a_memory_abort(tmp_path):
  await _pin(tmp_path, object_stream_bomb(), view=None, expected=(RuntimeError, 'SIGABRT'))


@pytest.mark.asyncio
async def test_tiny_text_object_bomb_is_a_memory_abort(tmp_path):
  await _pin(tmp_path, tiny_text_object_bomb(), view='text', expected=(RuntimeError, 'SIGABRT'))


@pytest.mark.asyncio
async def test_whole_page_links_hit_the_entry_cap(tmp_path):
  await _pin(tmp_path, whole_page_links(), view='links', expected=(ValueError, 'max_view_entries'))


@pytest.mark.asyncio
async def test_thousands_of_widgets_read_normally(tmp_path):
  def check(result) -> None:
    assert len(result.content['fields']) == 2000

  await _pin(tmp_path, many_widgets(), view='fields', expected=check)


@pytest.mark.asyncio
async def test_thousands_of_attachments_list_the_first_twenty(tmp_path):
  def check(result) -> None:
    assert result.content['attachment_count'] == 20000
    assert len(result.content['attachments']) == 20

  await _pin(tmp_path, many_attachments(), view='attachments', expected=check)


@pytest.mark.asyncio
async def test_nested_form_xobjects_render_nothing(tmp_path):
  await _pin(tmp_path, nested_form_xobjects(), view='text', expected=_text(''))


@pytest.mark.asyncio
async def test_huge_info_string_is_cut_to_the_title_limit(tmp_path):
  def check(result) -> None:
    assert result.details['title'] == 'A' * 200

  await _pin(tmp_path, huge_info_string(), view=None, expected=check)


@pytest.mark.asyncio
async def test_attachment_bomb_is_a_memory_abort(tmp_path):
  await _pin(tmp_path, attachment_bomb(), view='attachments', expected=(RuntimeError, 'SIGABRT'))


@pytest.mark.asyncio
async def test_text_typed_bomb_beside_a_real_attachment_fails_the_whole_view(tmp_path):
  await _pin(
    tmp_path,
    bomb_beside_real_attachment(),
    view='attachments',
    expected=(RuntimeError, 'SIGABRT'),
  )


_MUTANT_OUTCOMES: dict[str, tuple[type[Exception], str] | str] = {
  'truncated-half': (ValueError, 'not a readable PDF'),
  'truncated-before-eof': 'full',
  'seeded-flip-0': (ValueError, 'not a readable PDF'),
  'seeded-flip-1': 'full',
  'seeded-flip-2': (ValueError, 'not a readable PDF'),
  'seeded-flip-3': 'none',
}


@pytest.mark.asyncio
@pytest.mark.parametrize('name', list(_MUTANT_OUTCOMES))
async def test_seeded_mutants_and_truncations(tmp_path, name):
  outcome = _MUTANT_OUTCOMES[name]
  data = dict(mutants_and_truncations())[name]
  if isinstance(outcome, tuple):
    await _pin(tmp_path, data, view=None, expected=outcome)
  else:

    def check(result) -> None:
      assert result.details['text_layer'] == outcome

    await _pin(tmp_path, data, view=None, expected=check)
