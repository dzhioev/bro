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
  attachments: Sequence[tuple[str, bytes | Stream | None, str | None]] = (),
  links: Sequence[dict[str, Any]] = (),
  uri_base: bytes | None = None,
  fields: Sequence[dict[str, Any]] = (),
  xfa: bool = False,
  character_map: dict[int, str] | None = None,
  glyph_names: dict[int, str] | None = None,
  embedded_font: bytes | None = None,
  media_box: list[float] | None = None,
  page_tree_cycle: bool = False,
  rotation: int = 0,
  crop_box: list[float] | None = None,
  optional_content: bool = False,
  graphics_states: dict[str, Any] | None = None,
  unmapped_font: bool = False,
  form_content: bytes | None = None,
  form_matrix: list[float] | None = None,
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
  if unmapped_font:
    font_dictionary = {
      'Type': Name('Font'),
      'Subtype': Name('Type0'),
      'BaseFont': Name('Arial'),
      'Encoding': Name('Identity-H'),
      'DescendantFonts': [
        writer.add(
          {
            'Type': Name('Font'),
            'Subtype': Name('CIDFontType2'),
            'BaseFont': Name('Arial'),
            'CIDSystemInfo': {'Registry': b'Adobe', 'Ordering': b'Identity', 'Supplement': 0},
          }
        )
      ],
    }
  font = writer.add(font_dictionary)
  resources: dict[str, Any] = {'Font': {'F1': font}}
  layer = None
  if optional_content:
    layer = writer.add({'Type': Name('OCG'), 'Name': b'Fixture layer'})
    resources['Properties'] = {'MC0': layer}
  if graphics_states is not None:
    resources['ExtGState'] = graphics_states
  if form_content is not None:
    resources['XObject'] = {
      'Form': writer.add(
        Stream(
          form_content,
          {
            'Type': Name('XObject'),
            'Subtype': Name('Form'),
            'BBox': [0, 0, 612, 792],
            'Resources': {'Font': {'F1': font}},
            'Matrix': form_matrix or [1, 0, 0, 1, 0, 0],
          },
        )
      )
    }
  page_references = []
  widget_references = []
  for page_index, content in enumerate(['Hello PDF'] if pages is None else pages):
    page = writer.add()
    page_references.append(page)
    dictionary = {
      'Type': Name('Page'),
      'Parent': tree,
      'MediaBox': media_box or [0, 0, 612, 792],
      'Resources': resources,
      'Rotate': rotation,
    }
    if crop_box is not None:
      dictionary['CropBox'] = crop_box
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
            'Subtype': Name(link.get('subtype', 'Link')),
            'Rect': link.get('rectangle', [72, 715, 200, 735]),
            'F': link.get('flags', 0),
            **(
              {'A': link['action']}
              if 'action' in link
              else {'A': {'S': Name('URI'), 'URI': link.get('uri', b'https://example.test/')}}
            ),
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
  if uri_base is not None:
    catalog_dictionary['URI'] = {'Base': uri_base}
  if layer is not None:
    catalog_dictionary['OCProperties'] = {'OCGs': [layer], 'D': {'OFF': [layer]}}
  if attachments:
    names = []
    for name, data, media_type in attachments:
      stream_entries = {'Type': Name('EmbeddedFile')}
      if media_type is not None:
        stream_entries['Subtype'] = Name(media_type)
      filespec_entries = {
        'Type': Name('Filespec'),
        'F': text_string(name),
        'UF': text_string(name),
      }
      if data is not None:
        stream = (
          Stream(data.data, {**stream_entries, **data.entries})
          if isinstance(data, Stream)
          else Stream(data, stream_entries)
        )
        filespec_entries['EF'] = {'F': writer.add(stream)}
      filespec = writer.add(filespec_entries)
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


def text_view_cases(
  *, note_limit: int, excerpt_characters: int
) -> list[tuple[str, dict[str, Any], str, list[dict[str, Any]]]]:
  def hidden(reason: str, text: str) -> list[dict[str, Any]]:
    return [{'note': 'hidden_text', 'reason': reason, 'text': text}]

  cases: list[tuple[str, dict[str, Any], str, list[dict[str, Any]]]] = [
    ('empty', {'pages': [None]}, '', []),
    (
      'repeated-form',
      {
        'pages': [b'/Form Do q .1 0 0 .1 100 100 cm /Form Do Q'],
        'form_content': text_operators('same', x=100, y=100),
      },
      'same same',
      hidden('tiny', 'same'),
    ),
    (
      'wrapped',
      {'pages': [text_operators('ACCOUNT', y=720) + text_operators('NUMBER', y=705)]},
      'ACCOUNT\nNUMBER',
      [],
    ),
    (
      'table',
      {
        'pages': [
          b''.join(
            text_operators(value, x=x, y=y)
            for value, x, y in [
              ('Item', 72, 720),
              ('Price', 200, 720),
              ('Book', 72, 700),
              ('42.00', 200, 700),
            ]
          )
        ]
      },
      'Item Price\nBook 42.00',
      [],
    ),
    (
      'hyphen',
      {'pages': [b'BT /F1 12 Tf 72 720 Td (inter-) Tj 0 -15 Td (national) Tj ET']},
      'inter-national',
      [],
    ),
    (
      'crop',
      {'pages': [text_operators('outside', x=10)], 'crop_box': [100, 0, 612, 792]},
      'outside',
      hidden('outside_page', 'outside'),
    ),
    (
      'layer',
      {'pages': [b'/OC /MC0 BDC ' + text_operators('layer') + b' EMC'], 'optional_content': True},
      'layer',
      hidden('layer', 'layer'),
    ),
    (
      'form-small',
      {
        'pages': [b'/Form Do'],
        'form_content': text_operators('form', x=100, y=100),
        'form_matrix': [0.1, 0, 0, 0.1, 100, 100],
      },
      'form',
      hidden('tiny', 'form'),
    ),
    (
      'form-scaled',
      {
        'pages': [b'/Form Do'],
        'form_content': text_operators('form', x=10, y=10, size=1),
        'form_matrix': [12, 0, 0, 12, 0, 0],
      },
      'form',
      [],
    ),
    (
      'rotated',
      {'pages': [text_operators('first', y=720) + text_operators('second', y=700)], 'rotation': 90},
      'second\nfirst',
      [],
    ),
    ('rotated-box', {'pages': [text_operators('inside')], 'rotation': 90}, 'inside', []),
    (
      'iban',
      {
        'pages': ['ABCD'],
        'character_map': {65: 'DE89', 66: '\u200b', 67: '37040044', 68: '0532013000'},
      },
      'DE89370400440532013000',
      [],
    ),
    ('supplementary-pair', {'pages': ['A'], 'character_map': {65: '\U0001f600'}}, '\U0001f600', []),
    (
      'supplementary-scalar',
      {'pages': ['A'], 'glyph_names': {65: 'u1F600'}, 'embedded_font': decoy_font()},
      '\U0001f600',
      [],
    ),
    (
      'lying-map',
      {'pages': ['BANK'], 'character_map': {66: 'X', 65: 'Y', 78: 'Z', 75: '!'}},
      'XYZ!',
      [],
    ),
    (
      'tag',
      {
        'pages': ['ABC'],
        'character_map': {
          65: 'A',
          66: ''.join(chr(0xE0000 + ord(character)) for character in 'secret payload'),
          67: 'C',
        },
      },
      'AC',
      hidden('tag_characters', 'secret payload'),
    ),
    (
      'tag-spaces',
      {'pages': ['ABC'], 'character_map': {65: 'A', 66: '\U000e0020\U000e0020', 67: 'C'}},
      'AC',
      [],
    ),
    (
      'unmapped',
      {'pages': [b'BT /F1 12 Tf 72 720 Td <002b0048004f004f0052> Tj ET'], 'unmapped_font': True},
      '+HOOR',
      [{'note': 'unmapped_characters', 'character_count': 5}],
    ),
  ]
  for mode in range(8):
    cases.append(
      (
        f'mode-{mode}',
        {'pages': [text_operators('mode', mode=mode)]},
        'mode',
        hidden('invisible', 'mode') if mode in (3, 7) else [],
      )
    )
  for name, prefix, arguments, reason in [
    ('white-background', b'1 g ', {}, 'white'),
    ('dark-background', b'0 g 0 0 612 792 re f 1 g ', {}, 'white'),
    ('white-stroke', b'1 G ', {'mode': 1}, 'white'),
    ('black-stroke-white-fill', b'1 g 0 G ', {'mode': 1}, None),
    ('black-stroke-both', b'1 g 0 G ', {'mode': 2}, None),
    ('both-white', b'1 g 1 G ', {'mode': 2}, 'white'),
    ('near-white', b'0.97 g ', {}, None),
    ('white-threshold', b'0.99 g ', {}, 'white'),
    ('tiny', b'', {'size': 1}, 'tiny'),
    ('size-threshold', b'', {'size': 2}, None),
    ('negative-size', b'', {'size': -12}, None),
    ('outside', b'', {'x': 1000}, 'outside_page'),
    ('outside-before-tiny', b'1 g ', {'x': 1000, 'size': 1}, 'outside_page'),
    ('invisible-before-outside', b'', {'mode': 3, 'x': 1000}, 'invisible'),
  ]:
    cases.append(
      (
        name,
        {'pages': [prefix + text_operators('payload', **arguments)]},
        'payload',
        hidden(reason, 'payload') if reason else [],
      )
    )
  for name, state, mode, reason in [
    ('transparent-fill', {'ca': 0}, 0, 'invisible'),
    ('transparent-stroke', {'CA': 0}, 1, 'invisible'),
    ('transparent-both', {'ca': 0, 'CA': 0}, 2, 'invisible'),
    ('visible-stroke', {'ca': 0}, 2, None),
    ('white-transparent-stroke', {'CA': 0}, 2, 'white'),
  ]:
    cases.append(
      (
        name,
        {
          'pages': [b'/State gs 1 g ' + text_operators('alpha', mode=mode)],
          'graphics_states': {'State': state},
        },
        'alpha',
        hidden(reason, 'alpha') if reason else [],
      )
    )
  long_payload = 'x' * (excerpt_characters + 3)
  content = b''.join(
    b'q 1 g '
    + text_operators(long_payload, y=750 - 24 * index, size=3)
    + b' Q '
    + text_operators('visible', y=738 - 24 * index, size=3)
    for index in range(note_limit + 2)
  )
  cases.append(
    (
      'bounded-notes',
      {'pages': [content]},
      '\n'.join([long_payload, 'visible'] * (note_limit + 2)),
      hidden('white', long_payload[:excerpt_characters]) * note_limit
      + [{'note': 'hidden_text_more', 'run_count': 2}],
    )
  )
  return cases


def links_view_cases() -> list[tuple[str, dict[str, Any], list[dict[str, Any]]]]:
  def link(
    *,
    text='Download report',
    uri: str | None = 'https://annotation.test/opaque',
    hidden=False,
    page=1,
  ):
    return {'page_number': page, 'hidden': hidden, 'text': text, 'uri': uri}

  return [
    (
      'annotation-only',
      {'pages': ['Download report'], 'links': [{'uri': b'https://annotation.test/opaque'}]},
      [link()],
    ),
    (
      'hidden-anchors',
      {
        'pages': [
          text_operators('Download report')
          + text_operators('secret', mode=3, y=700)
          + b'1 g\n'
          + text_operators('white', y=680)
        ],
        'links': [{'rectangle': [0, 0, 612, 792]}],
      },
      [link(uri='https://example.test/')],
    ),
    (
      'generated-space',
      {'pages': [text_operators('Download') + text_operators('report', x=130)], 'links': [{}]},
      [link(uri='https://example.test/')],
    ),
    (
      'reversed-rectangle',
      {'pages': ['Download report'], 'links': [{'rectangle': [200, 735, 72, 715]}]},
      [link(uri='https://example.test/')],
    ),
    (
      'centre-not-intersection',
      {'pages': ['AB'], 'links': [{'rectangle': [78, 715, 80, 735]}]},
      [link(text='', uri='https://example.test/')],
    ),
    ('no-action', {'pages': ['Download report'], 'links': [{'action': None}]}, [link(uri=None)]),
    (
      'other-action',
      {'pages': ['Download report'], 'links': [{'action': {'S': Name('Launch'), 'F': b'program'}}]},
      [link(uri=None)],
    ),
    ('other-subtype', {'links': [{'subtype': 'Text'}]}, []),
    (
      'uri-bytes',
      {'pages': ['Download report'], 'links': [{'uri': b'custom: !~\x00\x1f\x7f\xc3\xa9\xff'}]},
      [link(uri='custom: !~%00%1F%7F%C3%A9%FF')],
    ),
    (
      'relative-base',
      {
        'pages': ['Download report'],
        'links': [{'uri': b'../x'}],
        'uri_base': b'https://base.test/dir/',
      },
      [link(uri='https://base.test/dir/../x')],
    ),
    (
      'base-no-slash',
      {
        'pages': ['Download report'],
        'links': [{'uri': b'child'}],
        'uri_base': b'https://base.test/dir',
      },
      [link(uri='https://base.test/dirchild')],
    ),
    (
      'unfiltered-scheme',
      {
        'pages': ['Download report'],
        'links': [{'uri': b'javascript:hello()'}, {'uri': b'file:///data'}],
      },
      [link(uri='javascript:hello()'), link(uri='file:///data')],
    ),
    (
      'rotation',
      {'pages': ['Download report'], 'links': [{}], 'rotation': 90},
      [link(uri='https://example.test/')],
    ),
    (
      'page-order',
      {
        'pages': ['First', 'Second'],
        'links': [{'page': 1}, {'uri': b'first:'}, {'uri': b'second:'}],
      },
      [
        link(text='First', uri='first:'),
        link(text='First', uri='second:'),
        link(text='Second', uri='https://example.test/', page=2),
      ],
    ),
    *[
      (
        f'flags-{flags}',
        {'pages': ['Download report'], 'links': [{'flags': flags}]},
        [link(hidden=hidden, uri='https://example.test/')],
      )
      for flags, hidden in [(1, False), (2, True), (32, True), (4, False)]
    ],
    *[
      (
        f'rectangle-{index}',
        {'pages': ['Download report'], 'links': [{'rectangle': rectangle}]},
        [link(text='', hidden=True, uri='https://example.test/')],
      )
      for index, rectangle in enumerate(
        [[0, 0, 0, 20], [0, 0, 20, 0], [700, 700, 800, 750], [0, 800, 200, 820]]
      )
    ],
  ]


def attachments_view_cases(
  text_bytes: int,
) -> list[tuple[str, list[tuple[str, bytes | Stream | None, str | None]], list[dict[str, Any]]]]:
  cases = [
    (
      'factur-x.xml',
      b'<?xml version="1.0"?>\r\n<Invoice>\n\t<Amount>42.00</Amount>\n</Invoice>\r\n',
      'application/xml',
      None,
    ),
    ('binary.xml', b'not read', 'application/octet-stream', 'not a declared text type'),
    ('large.txt', b'a' * (text_bytes + 1), 'text/plain', f'larger than {text_bytes // 1024} KiB'),
    ('missing.xml', None, None, 'no embedded file'),
    ('empty.txt', b'', None, None),
    ('bad-utf8.xml', b'\xff', None, 'not valid text'),
    ('controls.txt', b'A\x00B', None, 'not valid text'),
    ('c1.txt', 'A\u0085B'.encode(), None, 'not valid text'),
    ('invoice.bin', b'{"amount":42}', 'application/vnd.invoice+json', None),
    ('invoice.bin', b'<invoice/>', 'application/vnd.invoice+xml', None),
    ('invoice.bin', b'{"amount":42}', 'application/json', None),
    ('upper.TXT', b'plain', None, None),
    ('no-type.bin', b'plain', None, 'not a declared text type'),
    ('unknown.xml', b'plain', 'unknown/type', 'not a declared text type'),
    ('unicode.txt', 'A\u200b\u200d\U0001f600\ufeff'.encode(), 'text/plain', None),
  ]
  result = []
  for name, data, media_type, reason in cases:
    text = data.decode() if data is not None and reason is None else None
    if name == 'unicode.txt':
      text = 'A\u200d\U0001f600'
    result.append(
      (
        name,
        [(name, data, media_type)],
        [
          {
            'name': name,
            'media_type': media_type,
            'size_bytes': len(data)
            if data is not None and reason not in ('not a declared text type', 'no embedded file')
            else None,
            'text': text,
            'not_shown': reason,
          }
        ],
      )
    )
  return result
