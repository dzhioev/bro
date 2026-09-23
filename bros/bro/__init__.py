from bro.bro import BaseBro


class Bro(BaseBro):
  name = 'bro'
  description = 'your go-to bro: a generic agent with default settings'
  spells = ('ask.md', 'reflect.md', 'watch.md')
  system_prompt = 'You are a bro.'
