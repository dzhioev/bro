import pytest

from bro.procedures import parse_frontmatter


def _frontmatter(*lines: str) -> str:
  return '---\n' + '\n'.join(lines) + '\n---\n\nbody\n'


class TestParseFrontmatter:
  def test_absent_and_unterminated_frontmatter_pass_through(self):
    assert parse_frontmatter('# doc\n', 'source') == ({}, '# doc\n')
    assert parse_frontmatter('---\nname: land\n', 'source') == ({}, '---\nname: land\n')

  def test_inline_values_split_from_the_body(self):
    fields, body = parse_frontmatter(_frontmatter('name: land', 'version: 4.0.0'), 'source')
    assert fields == {'name': 'land', 'version': '4.0.0'}
    assert body == '\nbody\n'

  def test_value_keeps_colons_and_blank_lines_separate_keys(self):
    text = _frontmatter('name: land', '', 'summary: merges: the PR')
    assert parse_frontmatter(text, 'source')[0] == {'name': 'land', 'summary': 'merges: the PR'}

  def test_multi_line_value_folds_into_one_paragraph(self):
    text = _frontmatter('description:', '', 'first line', 'second line', '', 'version: 4.0.0')
    assert parse_frontmatter(text, 'source')[0] == {
      'description': 'first line second line',
      'version': '4.0.0',
    }

  def test_multi_line_value_may_close_on_the_fence(self):
    text = _frontmatter('description:', '', 'first line', 'second line')
    assert parse_frontmatter(text, 'source')[0] == {'description': 'first line second line'}

  @pytest.mark.parametrize(
    ('lines', 'match'),
    [
      (('description:', 'first line'), 'empty value'),
      (('description:',), 'empty value'),
      (('description:', '', '', 'version: 4.0.0'), 'empty multi-line value'),
      (('name land',), 'not `key: value`'),
      ((': land',), 'not `key: value`'),
      (('name: land', 'name: fix'), 'duplicate frontmatter key'),
      (('description:', '', 'first line', '', 'description: fix'), 'duplicate frontmatter key'),
    ],
  )
  def test_malformed_frontmatter_raises(self, lines, match):
    with pytest.raises(ValueError, match=match):
      parse_frontmatter(_frontmatter(*lines), 'source')
