"""Stdlib PDF fixture writer, including deliberately malformed documents."""

import hashlib
import struct
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Name:
  value: str


@dataclass(frozen=True)
class Reference:
  number: int


@dataclass
class Stream:
  data: bytes
  entries: dict[str, Any] = field(default_factory=dict)


_PADDING = bytes.fromhex('28bf4e5e4e758a4164004e56fffa01082e2e00b6d0683e802f0ca9fe6453697a')
_DOCUMENT_ID = b'bro-pdf-fixtures'


def rc4(key: bytes, data: bytes) -> bytes:
  state = list(range(256))
  position = 0
  for index in range(256):
    position = (position + state[index] + key[index % len(key)]) % 256
    state[index], state[position] = state[position], state[index]
  left = right = 0
  result = bytearray()
  for value in data:
    left = (left + 1) % 256
    right = (right + state[left]) % 256
    state[left], state[right] = state[right], state[left]
    result.append(value ^ state[(state[left] + state[right]) % 256])
  return bytes(result)


def _padded(password: str) -> bytes:
  return (password.encode('latin-1') + _PADDING)[:32]


def encryption(user: str, revision: int) -> tuple[dict[str, Any], bytes]:
  if revision not in (2, 3):
    raise ValueError('RC4 fixture revision must be 2 or 3')
  length = 5 if revision == 2 else 16
  digest = hashlib.md5(_padded('owner-password')).digest()
  if revision == 3:
    for _ in range(50):
      digest = hashlib.md5(digest).digest()
  owner_key = digest[:length]
  owner = rc4(owner_key, _padded(user))
  if revision == 3:
    for iteration in range(1, 20):
      owner = rc4(bytes(value ^ iteration for value in owner_key), owner)
  permissions = -44 if revision == 2 else -3904
  digest = hashlib.md5(
    _padded(user) + owner + struct.pack('<i', permissions) + _DOCUMENT_ID
  ).digest()
  if revision == 3:
    for _ in range(50):
      digest = hashlib.md5(digest[:length]).digest()
  key = digest[:length]
  if revision == 2:
    user_entry = rc4(key, _PADDING)
  else:
    user_entry = rc4(key, hashlib.md5(_PADDING + _DOCUMENT_ID).digest())
    for iteration in range(1, 20):
      user_entry = rc4(bytes(value ^ iteration for value in key), user_entry)
    user_entry += b'\0' * 16
  return {
    'Filter': Name('Standard'),
    'V': 1 if revision == 2 else 2,
    'R': revision,
    'Length': length * 8,
    'O': owner,
    'U': user_entry,
    'P': permissions,
  }, key


def text_string(value: str) -> bytes:
  return b'\xfe\xff' + value.encode('utf-16-be', errors='surrogatepass')


class PdfWriter:
  def __init__(self):
    self.objects: list[Any] = []

  def add(self, value: Any = None) -> Reference:
    self.objects.append(value)
    return Reference(len(self.objects))

  def set(self, reference: Reference, value: Any) -> None:
    self.objects[reference.number - 1] = value

  def build(
    self,
    root: Reference,
    *,
    info: Reference | None = None,
    user_password: str | None = None,
    revision: int = 2,
    security_handler: str | None = None,
    encryption_overrides: dict[str, Any] | None = None,
    xref_loop: bool = False,
  ) -> bytes:
    encryption_reference = None
    key = None
    if user_password is not None or security_handler is not None:
      declaration, key = encryption(user_password or '', revision)
      if security_handler is not None:
        declaration['Filter'] = Name(security_handler)
      declaration.update(encryption_overrides or {})
      encryption_reference = self.add(declaration)

    def serialize(value: Any, object_key: bytes | None) -> bytes:
      if isinstance(value, Reference):
        return f'{value.number} 0 R'.encode()
      if isinstance(value, Name):
        encoded = value.value.encode('utf-8')
        return b'/' + b''.join(
          bytes([byte])
          if 33 <= byte <= 126 and byte not in b'#/%()<>[]{}'
          else f'#{byte:02X}'.encode()
          for byte in encoded
        )
      if isinstance(value, Stream):
        data = rc4(object_key, value.data) if object_key else value.data
        return (
          serialize({**value.entries, 'Length': len(data)}, object_key)
          + b'\nstream\n'
          + data
          + b'\nendstream'
        )
      if isinstance(value, dict):
        return (
          b'<< '
          + b' '.join(
            serialize(Name(name), None) + b' ' + serialize(item, object_key)
            for name, item in value.items()
          )
          + b' >>'
        )
      if isinstance(value, list):
        return b'[' + b' '.join(serialize(item, object_key) for item in value) + b']'
      if isinstance(value, bytes):
        return b'<' + (rc4(object_key, value) if object_key else value).hex().encode() + b'>'
      if value is None:
        return b'null'
      if isinstance(value, bool):
        return b'true' if value else b'false'
      if isinstance(value, (int, float)):
        return str(value).encode()
      raise TypeError(f'unsupported PDF fixture value: {type(value)}')

    output = bytearray(b'%PDF-1.7\n%\xe2\xe3\xcf\xd3\n')
    offsets = [0]
    for number, value in enumerate(self.objects, 1):
      offsets.append(len(output))
      object_key = None
      if key is not None and Reference(number) != encryption_reference:
        object_key = hashlib.md5(key + number.to_bytes(3, 'little') + b'\0\0').digest()[
          : min(len(key) + 5, 16)
        ]
      output.extend(f'{number} 0 obj\n'.encode() + serialize(value, object_key) + b'\nendobj\n')
    start = len(output)
    output.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
      output.extend(f'{offset:010d} 00000 n \n'.encode())
    trailer: dict[str, Any] = {'Size': len(offsets), 'Root': root}
    if info is not None:
      trailer['Info'] = info
    if encryption_reference is not None:
      trailer.update(Encrypt=encryption_reference, ID=[_DOCUMENT_ID, _DOCUMENT_ID])
    if xref_loop:
      trailer['Prev'] = start
    output.extend(
      b'trailer\n' + serialize(trailer, None) + f'\nstartxref\n{start}\n%%EOF\n'.encode()
    )
    return bytes(output)


def text_operators(
  text: str, *, size: float = 12, mode: int = 0, x: float = 72, y: float = 720
) -> bytes:
  return (
    f'BT /F1 {size} Tf {mode} Tr {x} {y} Td <'.encode()
    + text.encode('latin-1').hex().encode()
    + b'> Tj ET\n'
  )


def make_pdf(
  pages: Sequence[str | bytes | None] | None = None,
  *,
  title: str | None = None,
  count: int | None = None,
  attachments: Sequence[tuple[str, bytes, str | None]] = (),
  links: Sequence[dict[str, Any]] = (),
  fields: Sequence[dict[str, Any]] = (),
  xfa: bool = False,
  character_map: dict[int, str] | None = None,
  glyph_names: dict[int, str] | None = None,
  embedded_font: bytes | None = None,
  media_box: list[float] | None = None,
  page_tree_cycle: bool = False,
  **build_arguments: Any,
) -> bytes:
  writer = PdfWriter()
  catalog = writer.add()
  tree = writer.add()
  font_dictionary: dict[str, Any] = {
    'Type': Name('Font'),
    'Subtype': Name('Type1'),
    'BaseFont': Name('Helvetica'),
  }
  if embedded_font is not None:
    descriptor = writer.add(
      {
        'Type': Name('FontDescriptor'),
        'FontName': Name('Arial'),
        'Flags': 32,
        'FontBBox': [0, 0, 600, 700],
        'ItalicAngle': 0,
        'Ascent': 800,
        'Descent': -200,
        'CapHeight': 700,
        'StemV': 80,
        'FontFile2': writer.add(Stream(embedded_font)),
      }
    )
    font_dictionary.update(
      Subtype=Name('TrueType'),
      BaseFont=Name('Arial'),
      FontDescriptor=descriptor,
      FirstChar=0,
      LastChar=255,
      Widths=[600] * 256,
    )
  if character_map is not None:
    mappings = b'\n'.join(
      f'<{code:02X}> <'.encode()
      + value.encode('utf-16-be', errors='surrogatepass').hex().encode()
      + b'>'
      for code, value in character_map.items()
    )
    cmap = (
      b'/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n'
      b'/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n'
      b'/CMapName /Fixture def /CMapType 2 def\n'
      b'1 begincodespacerange <00> <FF> endcodespacerange\n'
      + f'{len(character_map)} beginbfchar\n'.encode()
      + mappings
      + b'\nendbfchar endcmap CMapName currentdict /CMap defineresource pop end end'
    )
    font_dictionary['ToUnicode'] = writer.add(Stream(cmap))
  if glyph_names is not None:
    differences = []
    for code, name in glyph_names.items():
      differences.extend([code, Name(name)])
    font_dictionary['Encoding'] = {'Type': Name('Encoding'), 'Differences': differences}
  font = writer.add(font_dictionary)
  page_references = []
  widget_references = []
  for page_index, content in enumerate(['Hello PDF'] if pages is None else pages):
    page = writer.add()
    page_references.append(page)
    dictionary = {
      'Type': Name('Page'),
      'Parent': tree,
      'MediaBox': media_box or [0, 0, 612, 792],
      'Resources': {'Font': {'F1': font}},
    }
    if content is not None:
      dictionary['Contents'] = writer.add(
        Stream(text_operators(content) if isinstance(content, str) else content)
      )
    annotations = []
    for link in links:
      if link.get('page', 0) != page_index:
        continue
      annotations.append(
        writer.add(
          {
            'Type': Name('Annot'),
            'Subtype': Name('Link'),
            'Rect': link.get('rectangle', [72, 715, 200, 735]),
            'F': link.get('flags', 0),
            'A': {'S': Name('URI'), 'URI': link.get('uri', b'https://example.test/')},
          }
        )
      )
    for entry in fields:
      if entry.get('page', 0) != page_index:
        continue
      widget = writer.add(
        {
          'Type': Name('Annot'),
          'Subtype': Name('Widget'),
          'P': page,
          'FT': Name(entry.get('type', 'Tx')),
          'T': text_string(entry.get('name', 'field')),
          'V': text_string(entry.get('value', 'value')),
          'Rect': entry.get('rectangle', [150, 700, 300, 720]),
          'F': entry.get('flags', 0),
          'Ff': entry.get('field_flags', 0),
        }
      )
      annotations.append(widget)
      widget_references.append(widget)
    if annotations:
      dictionary['Annots'] = annotations
    writer.set(page, dictionary)
  writer.set(
    tree,
    {
      'Type': Name('Pages'),
      'Kids': [tree] if page_tree_cycle else page_references,
      'Count': len(page_references) if count is None else count,
    },
  )
  catalog_dictionary: dict[str, Any] = {'Type': Name('Catalog'), 'Pages': tree}
  if attachments:
    names = []
    for name, data, media_type in attachments:
      stream_entries = {'Type': Name('EmbeddedFile')}
      if media_type is not None:
        stream_entries['Subtype'] = Name(media_type)
      embedded = writer.add(Stream(data, stream_entries))
      filespec = writer.add(
        {
          'Type': Name('Filespec'),
          'F': text_string(name),
          'UF': text_string(name),
          'EF': {'F': embedded},
        }
      )
      names.extend([text_string(name), filespec])
    catalog_dictionary['Names'] = {'EmbeddedFiles': {'Names': names}}
  if widget_references or xfa:
    form: dict[str, Any] = {'Fields': widget_references}
    if xfa:
      form['XFA'] = writer.add(Stream(b'<xdp:xdp xmlns:xdp="http://ns.adobe.com/xdp/"/>'))
    catalog_dictionary['AcroForm'] = writer.add(form)
  writer.set(catalog, catalog_dictionary)
  info = writer.add({'Title': text_string(title)}) if title is not None else None
  return writer.build(catalog, info=info, **build_arguments)


def flate_stream(decoded_bytes: int, *, byte: bytes = b' ') -> Stream:
  compressor = zlib.compressobj()
  block = byte * min(decoded_bytes, 65536)
  chunks = []
  remaining = decoded_bytes
  while remaining:
    chunk = block[:remaining]
    chunks.append(compressor.compress(chunk))
    remaining -= len(chunk)
  chunks.append(compressor.flush())
  return Stream(b''.join(chunks), {'Filter': Name('FlateDecode')})


def decoy_font(*, advance: int = 1800) -> bytes:
  """A tiny Arial-named TrueType font with deliberately wide rectangle glyphs."""
  codes = [*range(32, 127), 0x1F600]
  glyph = struct.pack('>hhhhhHH', 1, 0, 0, 600, 700, 3, 0)
  glyph += b'\x01' * 4 + struct.pack('>hhhhhhhh', 0, 600, 0, -600, 0, 0, 700, 0)
  if len(glyph) % 2:
    glyph += b'\0'
  groups = b''.join(struct.pack('>III', code, code, 1) for code in codes)
  cmap = struct.pack('>HHIII', 12, 0, 16 + len(groups), 0, len(codes)) + groups
  names = [(1, 'Arial'), (2, 'Regular'), (4, 'Arial'), (6, 'Arial')]
  records = bytearray()
  strings = bytearray()
  for identifier, value in names:
    encoded = value.encode('utf-16-be')
    records.extend(struct.pack('>HHHHHH', 3, 1, 0x409, identifier, len(encoded), len(strings)))
    strings.extend(encoded)
  tables = {
    'head': struct.pack(
      '>IIIIHHqqhhhhHHhhh',
      0x10000,
      0x10000,
      0,
      0x5F0F3CF5,
      0,
      1000,
      0,
      0,
      0,
      0,
      600,
      700,
      0,
      8,
      2,
      0,
      0,
    ),
    'hhea': struct.pack(
      '>IhhhHhhhhhhhhhhhH', 0x10000, 800, -200, 0, advance, 0, 0, 600, 1, 0, 0, 0, 0, 0, 0, 0, 2
    ),
    'maxp': struct.pack('>I14H', 0x10000, 2, 4, 1, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0),
    'hmtx': struct.pack('>HhHh', advance, 0, advance, 0),
    'loca': struct.pack('>HHH', 0, len(glyph) // 2, len(glyph)),
    'glyf': glyph * 2,
    'cmap': struct.pack('>HHHHI', 0, 1, 3, 10, 12) + cmap,
    'name': struct.pack('>HHH', 0, len(names), 6 + len(records)) + records + strings,
    'post': struct.pack('>IIhhIIIII', 0x30000, 0, -100, 50, 0, 0, 0, 0, 0),
    'OS/2': struct.pack('>HhHHH', 0, advance, 400, 5, 0)
    + b'\0' * 52
    + struct.pack('>HHHhhhHH', 0x40, 32, 0xFFFF, 800, -200, 0, 800, 200),
  }
  count = len(tables)
  power = 2 ** (count.bit_length() - 1)
  header = struct.pack(
    '>IHHHH', 0x10000, count, power * 16, power.bit_length() - 1, count * 16 - power * 16
  )
  directory = bytearray()
  payload = bytearray()
  head_offset = 0
  for name, data in sorted(tables.items()):
    offset = len(header) + count * 16 + len(payload)
    padded = data + b'\0' * (-len(data) % 4)
    checksum = sum(struct.unpack(f'>{len(padded) // 4}I', padded)) & 0xFFFFFFFF
    directory.extend(name.encode() + struct.pack('>III', checksum, offset, len(data)))
    if name == 'head':
      head_offset = offset
    payload.extend(padded)
  font = bytearray(header + directory + payload)
  checksum = sum(struct.unpack(f'>{len(font) // 4}I', font)) & 0xFFFFFFFF
  struct.pack_into('>I', font, head_offset + 8, (0xB1B0AFBA - checksum) & 0xFFFFFFFF)
  return bytes(font)
