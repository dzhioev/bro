import abc
from typing import Any, Optional

class Bro(abc.ABC):
    @abc.abstractmethod
    async def tell(self, data: Any) -> None:
        '''Send data to the model.'''
        pass

    @abc.abstractmethod
    async def ask(self) -> Any:
        '''Get the model output.'''
        pass


class EchoBro(Bro):
    def __init__(self):
        self.data = None

    async def tell(self, data: Any) -> None:
        self.data = data

    async def ask(self) -> Any:
        return f'Hello, world!: "{self.data}"!'

def get_bro(type: Optional[str], *args, **kwargs) -> Bro:
    if type is None:
        return EchoBro()
    if type == 'chat_gpt':
        import bros.chat_gpt_bro
        return bros.chat_gpt_bro.ChatGPTBro(*args, **kwargs)
    raise ValueError(f'Unknown bro type: {type}')
