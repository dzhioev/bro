import pytest

from bro.files.text import sanitize, sanitize_strings, validate_strings


@pytest.mark.parametrize(
  ('first', 'last'),
  [
    (0x00AD, 0x00AD),
    (0x061C, 0x061C),
    (0x180E, 0x180E),
    (0x200B, 0x200B),
    (0x200E, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x206F),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0xE0000, 0xE007F),
  ],
)
def test_invisible_code_point_classes_are_removed(first, last):
  invisible = ''.join(map(chr, range(first, last + 1)))
  assert sanitize(f'DE89{invisible}3704').text == 'DE893704'


@pytest.mark.parametrize(
  'text', ['\u200c', '\u200d', 'می\u200cروم', '👩\u200d💻', '\u00a0', 'a\n\tb\r', '日本語']
)
def test_visible_text_and_language_joiners_remain(text):
  assert sanitize(text).text == text
  validate_strings(text)


def test_tag_payloads_preserve_original_spans_and_decode_only_printable_runs():
  def tags(text):
    return ''.join(chr(0xE0000 + ord(character)) for character in text)

  text = 'before' + tags('do this') + '\U000e007f' + tags(' ') + 'after' + tags('x')
  result = sanitize(text)
  assert result.text == 'beforeafter'
  assert [payload.text for payload in result.tag_payloads] == ['do this', 'x']
  for payload in result.tag_payloads:
    assert text[payload.start : payload.end] == tags(payload.text)


def test_nested_strings_and_keys_are_sanitized():
  assert sanitize_strings({'na\u200bme': [{'text': 'a\u202eb'}]}) == {'name': [{'text': 'ab'}]}
  with pytest.raises(ValueError, match='keys collide'):
    sanitize_strings({'a': 1, 'a\u200b': 2})


@pytest.mark.parametrize('code', [*range(0x20), *range(0x7F, 0xA0), 0xD800, 0xDBFF, 0xDC00, 0xDFFF])
def test_reply_string_controls_and_surrogates(code):
  text = chr(code)
  if text in '\t\n\r':
    validate_strings(text)
  else:
    with pytest.raises(ValueError, match='lone surrogate or control'):
      validate_strings({'nested': [text]})


def test_validation_includes_keys_and_nested_strings():
  with pytest.raises(ValueError, match='invalid reply string'):
    validate_strings({'bad\x00': None})
  with pytest.raises(ValueError, match='keys must be strings'):
    validate_strings({1: 'value'})
  validate_strings({'safe': [None, 3, True, '\U0001f600']})
