def init():
  # adding a new bro? import it here and add the class to the list below
  from bro.bros.assistant import Assistant
  from bro.bros.librorian import Librorian
  from bro.bros.pm import PM
  from bro.registry import register

  for bro_cls in [Assistant, PM, Librorian]:
    register(bro_cls)
