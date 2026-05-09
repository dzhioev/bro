from bro.bros.assistant import Assistant
from bro.bros.pm import PM
from bro.registry import register
from flow.mcp.bridge import create_flow_server

_flow_servers = [create_flow_server()]

register(Assistant, mcp_servers=_flow_servers)
register(PM, mcp_servers=_flow_servers)
