import bros.bro as bro
from bro.bro import BaseBro


class Bro(BaseBro):
  name = 'bro'
  description = 'your go-to bro: a generic agent with default settings'
  spells = (bro.spells.ask, bro.spells.reflect, bro.spells.watch)
  system_prompt = 'You are a bro.'
