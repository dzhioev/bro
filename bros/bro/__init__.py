from bro.bro import BaseBro
from bros.bro import spells


class Bro(BaseBro):
  name = 'bro'
  description = 'your go-to bro: a generic agent with default settings'
  spells = (spells.ask, spells.reflect, spells.watch)
  system_prompt = 'You are a bro.'
