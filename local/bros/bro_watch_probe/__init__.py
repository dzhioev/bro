from bro.mcp import ANY, shell
from bros.bro import Bro


class BroWatchProbe(Bro):
  name = 'bro-watch-probe'
  description = 'a bare bro with a shell, for the harness conformance probes'
  tools = [shell(ANY)]
