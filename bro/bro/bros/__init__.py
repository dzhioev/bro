def init():
  # adding a new bro? import it here and add the class to the list below
  from bro.bros.assistant import Assistant
  from bro.bros.bro import Bro
  from bro.bros.dev import Dev
  from bro.bros.devoops import Devoops
  from bro.bros.librorian import Librorian
  from bro.bros.pm import PM
  from bro.bros.ppp_dev import PPPDev
  from bro.registry import register

  for bro_cls in [Bro, Assistant, PM, Librorian, Devoops, Dev, PPPDev]:
    register(bro_cls)
