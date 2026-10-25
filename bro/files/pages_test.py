import pytest

from bro.files.pages import parse_pages, select_pages


@pytest.mark.parametrize(
  ('pages', 'expected'),
  [
    ('3', [3]),
    ('1-3,7', [1, 2, 3, 7]),
    ('7,1', [1, 7]),
    ('1-3,2-5', [1, 2, 3, 4, 5]),
    (None, list(range(1, 10))),
    ('9,9-9', [9]),
  ],
)
def test_page_selection_table(pages, expected):
  parsed = parse_pages(pages, max_pages=5000, paged=True)
  assert select_pages(parsed, page_count=9, max_pages=5000) == expected


@pytest.mark.parametrize(
  'pages',
  [
    '0',
    '3-1',
    '1-',
    '-3',
    '01',
    '1 - 3',
    '',
    '1,',
    ',1',
    '1,,2',
    '+3',
    '1-03',
    '1\n',
    '１',
    '١',
    '1-2-3',
    ' 1',
    '1\t',
  ],
)
def test_invalid_page_grammar(pages):
  with pytest.raises(ValueError):
    parse_pages(pages, max_pages=5000, paged=True)


@pytest.mark.parametrize('pages', ['1-99999', '5001', '9' * 10000, '1-2,5001'])
def test_bound_checked_before_expansion_or_integer_conversion(pages):
  with pytest.raises(ValueError, match='limit of 5000'):
    parse_pages(pages, max_pages=5000, paged=True)


def test_page_beyond_count_names_count():
  parsed = parse_pages('10', max_pages=5000, paged=True)
  with pytest.raises(ValueError, match='document has 9 pages'):
    select_pages(parsed, page_count=9, max_pages=5000)


@pytest.mark.parametrize(('max_pages', 'paged'), [(None, False), (5000, False), (None, True)])
def test_pages_rejected_for_unpaged_view_or_type(max_pages, paged):
  assert parse_pages(None, max_pages=max_pages, paged=paged) is None
  with pytest.raises(ValueError, match='takes no pages; omit pages'):
    parse_pages('1', max_pages=max_pages, paged=paged)


@pytest.mark.parametrize(('count', 'message'), [(0, 'no pages'), (5001, 'above the limit')])
def test_document_page_count_is_bounded(count, message):
  with pytest.raises(ValueError, match=message):
    select_pages(None, page_count=count, max_pages=5000)
