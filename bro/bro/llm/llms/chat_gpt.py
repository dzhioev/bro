from bro import Bro
from openai import OpenAI


class ChatGPTBro(Bro):
    def __init__(self, api_key: str):
        self.client = OpenAI(api_key=api_key)

    async def tell(self, prompt: str) -> None:
        self.response = self.client.chat.completions.create(
            model="gpt-4-turbo",
            messages=[{"role": "user", "content": prompt},],
            max_tokens=500)

    async def ask(self) -> str:
        return self.response.choices[0].message.content