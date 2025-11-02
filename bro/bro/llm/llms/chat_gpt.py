import bro.bro
import configs
import json
import os
from openai import OpenAI

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
    self.response = self.client.chat.completions.create(
      model='gpt-4-turbo',
      messages=[
        {'role': 'user', 'content': phrase},
      ],
      max_tokens=500,
    )

  async def ask(self) -> str:
    return self.response.choices[0].message.content
