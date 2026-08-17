"""tests for display/_yaml.py — YAML rendering and its fidelity to the value rendered."""

from typing import Any

import pytest
import yaml

from bro.trails.display._yaml import block, flow, render

WIDTH = 40


def test_flow_drops_the_quotes_json_needs():
  assert flow({'file_path': '/workspace/bro.py', 'limit': 240}) == (
    '{file_path: /workspace/bro.py, limit: 240}'
  )


def test_flow_quotes_what_a_parser_would_read_as_another_type():
  assert flow({'version': '1.0', 'enabled': 'yes', 'day': '2026-08-17', 'mask': '0x10'}) == (
    '{version: "1.0", enabled: "yes", day: "2026-08-17", mask: "0x10"}'
  )


def test_flow_quotes_what_its_own_punctuation_would_swallow():
  assert flow(['a, b', 'k: v', '[x]']) == '["a, b", "k: v", "[x]"]'


def test_block_carries_line_breaks_as_literal_scalars():
  assert block({'command': 'cd /workspace\ngit status\n', 'limit': 300}) == (
    'command: |\n  cd /workspace\n  git status\nlimit: 300\n'
  )


def test_block_spells_out_the_indentation_of_content_opening_on_whitespace():
  assert block({'old_string': '  if missing:\n    raise TrailNotFound\n'}) == (
    'old_string: |2\n    if missing:\n      raise TrailNotFound\n'
  )


def test_block_nests_collections_under_their_key():
  assert block({'todos': [{'content': 'ship', 'status': 'done'}], 'count': 1}) == (
    'todos:\n  - content: ship\n    status: done\ncount: 1\n'
  )


def test_render_stays_on_one_line_while_it_fits():
  assert render({'limit': 300}, width=WIDTH) == '{limit: 300}'


def test_render_expands_past_the_width():
  value = {'file_path': '/workspace/bro/trails/display/core.py'}
  assert render(value, width=WIDTH) == 'file_path: /workspace/bro/trails/display/core.py\n'


def test_render_expands_for_a_line_break_however_short():
  assert render({'body': 'a\nb'}, width=WIDTH) == 'body: |-\n  a\n  b\n'


def test_a_value_with_no_yaml_rendering_is_refused():
  with pytest.raises(TypeError):
    flow({'when': object()})
  with pytest.raises(ValueError):
    flow({'ratio': float('inf')})
  with pytest.raises(TypeError):
    flow({1: 'numbered'})


_SCALARS = (
  '',
  ' ',
  'plain',
  'yes',
  'null',
  '~',
  '1.0',
  '0x10',
  '2026-08-17',
  '-3',
  '- item',
  '--include=*.py',
  '#comment',
  '?maybe',
  ':starts',
  '<<',
  '=',
  'key: value',
  'key:value',
  'ends with:',
  'trailing ',
  '\tindented',
  'a\nb',
  'a\n\nb',
  'ends\n',
  'ends\n\n',
  '\n',
  ' opens with space\nand continues',
  'code:\n  return 1\n',
  'line\n   \nblank-but-for-spaces',
  'carriage\rreturn',
  'null byte \x00 here',
  'ünïcöde — em dash',
  '{"nested": "json"}',
  1,
  0,
  -12,
  1.5,
  1e-05,
  1e20,
  True,
  False,
  None,
)


# every scalar is rendered in each position an entry can hold — opening, closing, nested,
# keyed — since what a form may carry depends on where it sits
_NEIGHBOURS = ('plain', '', 'a\nb', 'ends\n', 1, None)


def _corpus() -> list[Any]:
  values: list[Any] = [*_SCALARS, {}, [], {'k': {}}, {'k': []}, [[]], [{}]]
  for scalar in _SCALARS:
    for neighbour in _NEIGHBOURS:
      values.append({'a': scalar, 'b': neighbour})
      values.append({'a': neighbour, 'b': scalar})
      values.append([scalar, neighbour])
      values.append({'a': {'b': scalar}, 'c': [neighbour]})
    if isinstance(scalar, str) and len(scalar) > 0:
      values.append({scalar: 'keyed'})
  return values


@pytest.mark.parametrize('renderer', [flow, block, lambda value: render(value, width=WIDTH)])
def test_every_rendering_parses_back_as_the_value_it_was_given(renderer):
  for value in _corpus():
    assert yaml.safe_load(renderer(value)) == value, renderer(value)


def test_flow_renderings_are_one_line():
  for value in _corpus():
    assert '\n' not in flow(value)
