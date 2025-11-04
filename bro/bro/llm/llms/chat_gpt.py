import bro.bro
import configs

from openai import OpenAI

import json
import os

DEFAULT_CONFIG_PATH = os.path.join(configs.DEFAULT_CONFIGS_DIR, 'openai.json')


class ChatGPTBro(bro.bro.Bro):
  @staticmethod
  def create(config_path=DEFAULT_CONFIG_PATH):
    with open(config_path, 'r') as f:
      config = json.load(f)
    return ChatGPTBro(api_key=config['api_key'])

  def __init__(self, api_key: str):
    self.client = OpenAI(api_key=api_key)

  async def tell(self, phrase: str) -> None:
    self.response = self.client.responses.create(
      model='gpt-4-turbo',
      input=[
        {
          'role': 'user',
          'content': [
            { 'type': 'input_text', 'text': phrase }
          ]
        }
      ],
      max_output_tokens=10000
    )

  async def ask(self) -> str:
    return str(self.response.output[0].content)
