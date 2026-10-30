"""Stdlib-only hostile PDF fixture generators for the hostile corpus.

Each function builds one document of a hostile family, sized to finish in
seconds through the real confined runner. These functions only build bytes;
the corpus test pins the outcome of each family's first run.
"""

import random
import zlib

from bro.pdf.pdf_test_helper import Name, PdfWriter, Stream, flate_stream, make_pdf

# A Flate stream this large inflates past the worker's 1 GiB address-space bound.
GIBI_INFLATION = 1500 * 1024**2

# Text objects placed far enough apart that PDFium keeps every character.
TEXT_OBJECT_STEP = 50
TEXT_OBJECT_COUNT = 200_000


def flate_content_bomb() -> bytes:
  """A page whose content stream inflates past the address-space bound."""
  writer = PdfWriter()
  catalog = writer.add()
  tree = writer.add()
  page = writer.add()
  contents = writer.add(flate_stream(GIBI_INFLATION, byte=b' '))
  writer.set(
    page,
    {
      'Type': Name('Page'),
      'Parent': tree,
      'MediaBox': [0, 0, 612, 792],
      'Resources': {},
      'Contents': contents,
    },
  )
  writer.set(tree, {'Type': Name('Pages'), 'Kids': [page], 'Count': 1})
  writer.set(catalog, {'Type': Name('Catalog'), 'Pages': tree})
  return writer.build(catalog)


def page_tree_cycle() -> bytes:
  """A page tree whose only kid is itself; the single page is unreachable."""
  return make_pdf(page_tree_cycle=True)


def xref_prev_loop() -> bytes:
  """A trailer whose /Prev points back at its own xref section."""
  return make_pdf(xref_loop=True)


def deep_page_tree(depth: int = 5000) -> bytes:
  """A page tree nested `depth` levels deep before it reaches the page."""
  writer = PdfWriter()
  catalog = writer.add()
  page = writer.add()
  font = writer.add({'Type': Name('Font'), 'Subtype': Name('Type1'), 'BaseFont': Name('Helvetica')})
  nodes = [writer.add() for _ in range(depth)]
  writer.set(
    page,
    {
      'Type': Name('Page'),
      'Parent': nodes[-1],
      'MediaBox': [0, 0, 612, 792],
      'Resources': {'Font': {'F1': font}},
      'Contents': writer.add(Stream(b'BT /F1 9 Tf 72 720 Td (x) Tj ET')),
    },
  )
  for index in range(depth):
    kid = page if index == depth - 1 else nodes[index + 1]
    writer.set(nodes[index], {'Type': Name('Pages'), 'Kids': [kid], 'Count': 1})
  writer.set(catalog, {'Type': Name('Catalog'), 'Pages': nodes[0]})
  return writer.build(catalog)


# A real number written as a fixed-point token so PDFium keeps it, not an
# exponent form that its real parser rejects into a default page box.
_HUGE_COORDINATE = '1000000000000000000000000000000.0'


def huge_media_box() -> bytes:
  """A page whose MediaBox is near the limits of a double's magnitude."""
  content = b'BT /F1 12 Tf 1000 720 Td (x) Tj ET\n'
  box = f'-{_HUGE_COORDINATE} -{_HUGE_COORDINATE} {_HUGE_COORDINATE} {_HUGE_COORDINATE}'.encode()
  return (
    b'%PDF-1.7\n'
    b'1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n'
    b'2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n'
    b'3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox ['
    + box
    + b'] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n'
    b'4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n'
    b'5 0 obj\n<< /Length '
    + str(len(content)).encode()
    + b' >>\nstream\n'
    + content
    + b'endstream\nendobj\n'
    b'trailer << /Root 1 0 R /Size 6 >>\n'
    b'startxref\n0\n%%EOF\n'
  )


def many_pages(count: int = 5000) -> bytes:
  """A document at the page-count limit, every page carrying text."""
  return make_pdf(['x'] * count)


def oversized_count() -> bytes:
  """One page whose /Count claims more pages than the limit allows."""
  return make_pdf(['x'], count=5001)


def _compress_with_padding(prefix: bytes, total: int, byte: bytes = b' ') -> bytes:
  """Flate-compress `prefix` plus `total - len(prefix)` bytes, chunked."""
  compressor = zlib.compressobj()
  chunks = [compressor.compress(prefix)]
  remaining = total - len(prefix)
  block = byte * min(remaining, 65536)
  while remaining:
    chunk = block[:remaining]
    chunks.append(compressor.compress(chunk))
    remaining -= len(chunk)
  chunks.append(compressor.flush())
  return b''.join(chunks)


def object_stream_bomb() -> bytes:
  """An /ObjStm that inflates past the bound when its compressed object resolves."""
  header = b'1 8\n5 0\n'
  body = b'<< /Type /Pages /Kids [] /Count 0 >>'
  compressed = _compress_with_padding(header + body, GIBI_INFLATION)
  parts = [
    b'%PDF-1.7\n',
    b'1 0 obj\n<< /Type /Catalog /Pages 5 0 R >>\nendobj\n',
    b'2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n',
    b'3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n',
    b'4 0 obj\n<< /Type /ObjStm /N 1 /First 8 /Filter /FlateDecode /Length '
    + str(len(compressed)).encode()
    + b' >>\nstream\n'
    + compressed
    + b'\nendstream\nendobj\n',
  ]
  offsets = []
  running = len(parts[0])
  for part in parts[1:5]:
    offsets.append(running)
    running += len(part)
  parts.append(b'xref\n0 6\n0000000000 65535 f \n')
  parts.append(b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets))
  parts.append(b'0000000004 00000 n \n')
  parts.append(
    b'trailer << /Root 1 0 R /Size 6 >>\nstartxref\n' + str(running).encode() + b'\n%%EOF\n'
  )
  return b''.join(parts)


def tiny_text_object_bomb() -> bytes:
  """A page of hundreds of thousands of tiny text objects at distinct spots."""
  content = b''.join(
    f'BT /F1 9 Tf {72 + index * TEXT_OBJECT_STEP} 720 Td (12345678901234567890) Tj ET\n'.encode()
    for index in range(TEXT_OBJECT_COUNT)
  )
  return make_pdf([content])


def whole_page_links(count: int = 20001) -> bytes:
  """A page whose links all carry a whole-page rectangle, over the entry cap."""
  return make_pdf(['hello world'], links=[{'rectangle': [0, 0, 612, 792]} for _ in range(count)])


def many_widgets(count: int = 2000) -> bytes:
  """A form with thousands of text widgets on one page."""
  return make_pdf([None], fields=[{'name': f'field-{index}'} for index in range(count)])


def many_attachments(count: int = 20000) -> bytes:
  """A name tree with thousands of embedded files."""
  return make_pdf(
    ['x'], attachments=[(f'file-{index}.txt', b'a', 'text/plain') for index in range(count)]
  )


def nested_form_xobjects(depth: int = 1000) -> bytes:
  """Form XObjects nested `depth` deep, each drawing the next."""
  writer = PdfWriter()
  catalog = writer.add()
  tree = writer.add()
  page = writer.add()
  font = writer.add({'Type': Name('Font'), 'Subtype': Name('Type1'), 'BaseFont': Name('Helvetica')})
  forms = [writer.add() for _ in range(depth)]
  for index in range(depth):
    if index + 1 < depth:
      content = f'/Fm{index + 1} Do'.encode()
      resources = {'XObject': {f'Fm{index + 1}': forms[index + 1]}}
    else:
      content = b'BT /F1 9 Tf 72 720 Td (deep) Tj ET'
      resources = {'Font': {'F1': font}}
    writer.set(
      forms[index],
      Stream(
        content,
        {
          'Type': Name('XObject'),
          'Subtype': Name('Form'),
          'BBox': [0, 0, 612, 792],
          'Resources': resources,
        },
      ),
    )
  writer.set(
    page,
    {
      'Type': Name('Page'),
      'Parent': tree,
      'MediaBox': [0, 0, 612, 792],
      'Resources': {'XObject': {'Fm0': forms[0]}},
      'Contents': writer.add(Stream(b'/Fm0 Do')),
    },
  )
  writer.set(tree, {'Type': Name('Pages'), 'Kids': [page], 'Count': 1})
  writer.set(catalog, {'Type': Name('Catalog'), 'Pages': tree})
  return writer.build(catalog)


def huge_info_string() -> bytes:
  """A Title string of millions of characters in the info dictionary."""
  return make_pdf(['x'], title='A' * (2 * 1024 * 1024))


def attachment_bomb() -> bytes:
  """A text-typed attachment whose stream inflates past the bound."""
  return make_pdf(attachments=[('bomb.xml', flate_stream(GIBI_INFLATION), 'application/xml')])


def bomb_beside_real_attachment() -> bytes:
  """A real Factur-X attachment followed by an inflating text-typed bomb."""
  return make_pdf(
    attachments=[
      ('factur-x.xml', b'<invoice/>', 'application/xml'),
      ('bomb.xml', flate_stream(GIBI_INFLATION), 'application/xml'),
    ]
  )


def _flip(data: bytes, offset: int) -> bytes:
  mutated = bytearray(data)
  mutated[offset] ^= 0xFF
  return bytes(mutated)


def mutants_and_truncations(seed: int = 20261030) -> list[tuple[str, bytes]]:
  """Truncations and seeded byte flips of a valid document, all deterministic."""
  base = make_pdf(['hello world'])
  cases = [
    ('truncated-half', base[: len(base) // 2]),
    ('truncated-before-eof', base[:-10]),
  ]
  rng = random.Random(seed)
  for index in range(4):
    offset = rng.randrange(15, len(base))
    cases.append((f'seeded-flip-{index}', _flip(base, offset)))
  return cases
