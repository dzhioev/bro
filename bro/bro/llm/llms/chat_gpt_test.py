from llm.llms.chat_gpt import _make_strict_schema


class TestMakeStrictSchema:
  def test_top_level_object_gets_required_and_no_additional(self):
    out = _make_strict_schema(
      {'type': 'object', 'properties': {'a': {'type': 'string'}, 'b': {'type': 'integer'}}}
    )
    assert out['required'] == ['a', 'b']
    assert out['additionalProperties'] is False

  def test_nested_def_object_gets_required(self):
    # OpenAI strict mode rejects nested object schemas without `required` listing
    # every property — even when referenced via $defs from an anyOf branch
    schema = {
      'type': 'object',
      'properties': {
        'filters': {
          'anyOf': [{'$ref': '#/$defs/F'}, {'type': 'null'}],
          'default': None,
        }
      },
      '$defs': {
        'F': {
          'type': 'object',
          'properties': {
            'a': {'type': 'string'},
            'b': {'anyOf': [{'type': 'integer'}, {'type': 'null'}]},
          },
        }
      },
    }
    out = _make_strict_schema(schema)
    f = out['$defs']['F']
    assert f['required'] == ['a', 'b']
    assert f['additionalProperties'] is False

  def test_recurses_into_anyof_branches(self):
    out = _make_strict_schema(
      {
        'anyOf': [
          {'type': 'object', 'properties': {'x': {'type': 'string'}}},
          {'type': 'null'},
        ]
      }
    )
    obj_branch = out['anyOf'][0]
    assert obj_branch['required'] == ['x']
    assert obj_branch['additionalProperties'] is False

  def test_recurses_into_array_items(self):
    out = _make_strict_schema(
      {
        'type': 'object',
        'properties': {
          'xs': {
            'type': 'array',
            'items': {'type': 'object', 'properties': {'n': {'type': 'integer'}}},
          }
        },
      }
    )
    items = out['properties']['xs']['items']
    assert items['required'] == ['n']
    assert items['additionalProperties'] is False

  def test_non_object_schema_passes_through_unchanged(self):
    out = _make_strict_schema({'type': 'string', 'description': 'hello'})
    assert out == {'type': 'string', 'description': 'hello'}

  def test_input_not_mutated(self):
    schema = {'type': 'object', 'properties': {'a': {'type': 'string'}}}
    _make_strict_schema(schema)
    assert 'required' not in schema
    assert 'additionalProperties' not in schema
