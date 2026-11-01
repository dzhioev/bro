from bro.bro import BaseBro
from bros.bro import Bro
from bros.terminal import Terminal


def test_terminal_opts_out_of_the_shared_bro_defaults():
  terminal = Terminal()
  assert isinstance(terminal, BaseBro)
  assert not isinstance(terminal, Bro)
  assert terminal.reach().sources == ()
  assert terminal._features == {}
  assert terminal.spell_descriptions() == []
