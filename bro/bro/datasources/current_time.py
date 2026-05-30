from base.time_util import LOCAL_TZ_NAME, local_tz, utc_now
from bro.datasources.base import DataSource
from llm.mcp import FunctionTool, InProcessMCPServer, MCPServer


class CurrentTime(DataSource):
  name = 'current-time'
  summary = (
    'Current local date and time. Use to anchor any "now", "recent", "latest", or '
    'year-relative reasoning against the actual current moment rather than guessing.'
  )

  def as_mcp_server(self) -> MCPServer:
    return InProcessMCPServer(
      [
        FunctionTool(
          self.get_time,
          name=f'{self.name}-get-time',
          description='return the current local date and time',
        )
      ]
    )

  def get_time(self) -> str:
    now = utc_now().astimezone(local_tz())
    return f'{now.isoformat(timespec="seconds")} ({LOCAL_TZ_NAME})'
