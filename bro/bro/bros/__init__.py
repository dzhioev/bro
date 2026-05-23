def init():
  from bro.bros.assistant import Assistant
  from bro.bros.librorian import Librorian
  from bro.bros.pm import PM
  from bro.registry import register
  from flow.mcp.bridge import create_flow_server

  flow_servers = [create_flow_server()]
  register(Assistant, mcp_servers=flow_servers)
  register(PM, mcp_servers=flow_servers)
  register(Librorian)
