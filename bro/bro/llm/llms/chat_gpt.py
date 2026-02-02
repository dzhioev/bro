import bro.bro
import configs

from openai import OpenAI
from openai.types.responses import Response, ResponseOutputItem, ResponseOutputMessage

import json
import os
import base64

DEFAULT_CONFIG_PATH = os.path.join(configs.DEFAULT_CONFIGS_DIR, 'openai.json')


def encode_file(path: str) -> str:
  with open(path, 'rb') as f:
    return base64.b64encode(f.read()).decode('utf-8')


def image_to_content(image_path: str) -> dict[str, str]:
  if not image_path.endswith('.png'):
    raise NotImplementedError('only PNG images supported')
  encoded_image = encode_file(image_path)
  image_url = f'data:image/png;base64,{encoded_image}'
  return {'type': 'input_image', 'image_url': image_url, 'detail': 'high'}


def text_to_content(text: str) -> dict[str, str]:
  return {'type': 'input_text', 'text': text}


def extract_only_message(output: list[ResponseOutputItem]) -> ResponseOutputMessage:
  result = None
  for item in output:
    if item.type == 'message':
      if result is not None:
        raise NotImplementedError(
          f'output contains more then one output messages. output: {output}'
        )
      result = item
  if result is None:
    raise RuntimeError(f"output doesn't contain output messages. output: {output}")
  return result


def parse_response(response: Response) -> str:
  message = extract_only_message(response.output)
  chunks = []
  for item in message.content:
    if item.type == 'refusal':
      raise RuntimeError('got refusal: {item.refusal}')
    assert item.type == 'output_text'
    chunks.append(item.text)
  if len(chunks) == 0:
    raise RuntimeError('no output texts in message: {response.message}')
  return ''.join(chunks)


class ChatGPTBro(bro.bro.Bro):
  @staticmethod
  def create(config_path=DEFAULT_CONFIG_PATH):
    with open(config_path, 'r') as f:
      config = json.load(f)
    return ChatGPTBro(api_key=config['api_key'])

  def __init__(self, api_key: str):
    self.client = OpenAI(api_key=api_key)

  async def tell(self, phrase: str, images: list[str] | None) -> None:
    content = []
    if images is not None:
      for image_path in images:
        content.append(image_to_content(image_path))
    content.append(text_to_content(phrase))

    response = self.client.responses.create(
      model='gpt-5', input=[{'role': 'user', 'content': content}]
    )
    self.text_response = parse_response(response)

  async def ask(self) -> str:
    return self.text_response
