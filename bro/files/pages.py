"""Bounded, one-based page selections."""

import re
from typing import Optional

_ITEM = re.compile(r'([1-9][0-9]*)(?:-([1-9][0-9]*))?')


def parse_pages(
  pages: Optional[str], *, max_pages: Optional[int], paged: bool
) -> Optional[list[int]]:
  """Parse without a document count; None denotes all pages."""
  if pages is None:
    return None
  if not paged or max_pages is None:
    raise ValueError('this view takes no pages; omit pages')
  intervals: list[tuple[int, int]] = []
  for item in pages.split(','):
    match = _ITEM.fullmatch(item)
    if match is None:
      raise ValueError(
        'invalid pages; use positive page numbers or ranges such as 1-3,7, without spaces or leading zeros'
      )
    endpoints = (match[1], match[2] or match[1])
    bound = str(max_pages)
    if any(
      len(number) > len(bound) or (len(number) == len(bound) and number > bound)
      for number in endpoints
    ):
      raise ValueError(f'pages exceeds the limit of {max_pages}; narrow pages')
    first, last = map(int, endpoints)
    if first > last:
      raise ValueError('descending page range; put the first page before the last')
    intervals.append((first, last))
  return sorted({page for first, last in intervals for page in range(first, last + 1)})


def select_pages(pages: Optional[list[int]], *, page_count: int, max_pages: int) -> list[int]:
  """Resolve a parsed selection once the provider knows the document count."""
  if page_count < 1:
    raise ValueError('the document has no pages; provide a document with pages')
  if page_count > max_pages:
    raise ValueError(
      f'the document has {page_count} pages, above the limit of {max_pages}; use a smaller document'
    )
  if pages is None:
    return list(range(1, page_count + 1))
  if any(page < 1 or page > page_count for page in pages):
    raise ValueError(
      f'page beyond the document count; the document has {page_count} pages; narrow pages'
    )
  return sorted(set(pages))
