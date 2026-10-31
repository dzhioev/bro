from bro.llm.llm import LLMSpec
from bro.llm.wire import Content, File, Text, require_accepted


def encode_content(recipe: LLMSpec, content: tuple[Content, ...]) -> str | list[dict]:
  require_accepted(recipe, content)
  if len(content) == 1 and isinstance(content[0], Text):
    return content[0].text
  return [encode_item(item) for item in content]


def encode_item(item: Content) -> dict:
  if isinstance(item, Text):
    return {'type': 'input_text', 'text': item.text}
  if isinstance(item, File):
    if item.mime_type.startswith('image/'):
      return {'type': 'input_image', 'image_url': item.data_url(), 'detail': 'auto'}
    return {
      'type': 'input_file',
      'file_data': item.data_url(),
      'filename': item.filename or 'file',
      'detail': 'auto',
    }
  raise TypeError(f'unsupported content item: {type(item).__name__}')
