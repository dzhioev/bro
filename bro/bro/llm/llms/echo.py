import bro.bro


class EchoBro(bro.bro.Bro):
  @staticmethod
  def create():
    return EchoBro()

  def __init__(self):
    self.phrase = None
    self.images = None

  async def tell(self, phrase: str, images: list | None) -> None:
    self.phrase = phrase
    self.images = images

  async def ask(self) -> str:
    result = f'{self.phrase}'
    if self.images is not None and len(self.images) > 0:
      result += f' +({len(self.images)} images)'
    return result


