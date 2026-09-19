"""chat text sized to the journal's message bound, for the tests that probe it."""

import json

from bro.broker.journal import MAX_MESSAGE_BYTES


def text_at_the_message_bound() -> str:
  """the longest ascii text whose `{'text': …}` payload fits `MAX_MESSAGE_BYTES` exactly."""
  overhead = len(json.dumps({'text': ''}, separators=(',', ':')).encode())
  return 'x' * (MAX_MESSAGE_BYTES - overhead)
