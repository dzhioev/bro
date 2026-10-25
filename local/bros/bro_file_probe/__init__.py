from bro.files import mcp as file_mcp
from bro.mcp import mount
from bros.bro import Bro


class BroFileProbe(Bro):
  name = 'bro-file-probe'
  description = 'a file reader for the harness conformance probes'
  tools = [mount(file_mcp.toolset)]
