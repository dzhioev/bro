"""Messages API records without the SDK."""

from bro.llm.records import Call, DecodedCall, Message, Part, Reasoning, Stop, Unknown

NAME = 'messages'
CHAINING = 'client'


def decode(response: dict) -> DecodedCall:
  parts: list[Part] = []
  content = response.get('content')
  if not isinstance(content, list):
    parts.append(Unknown(1, response))
  else:
    for index, block in enumerate(content, start=1):
      if not isinstance(block, dict):
        parts.append(Unknown(index, block))
      elif block.get('type') == 'thinking' and isinstance(block.get('thinking'), str):
        if block['thinking']:
          parts.append(Reasoning(index, block['thinking']))
      elif block.get('type') == 'text' and isinstance(block.get('text'), str):
        parts.append(Message(index, block['text']))
      elif block.get('type') == 'tool_use':
        parts.append(Call.parse(index, block.get('id'), block.get('name'), block.get('input')))
      else:
        parts.append(Unknown(index, block))
  reason = response.get('stop_reason')
  stop: Stop = (
    'truncated' if reason == 'max_tokens' else 'filtered' if reason == 'refusal' else 'completed'
  )
  raw_usage = response.get('usage')
  return DecodedCall(
    model=str(response.get('model', 'unknown')),
    usage=raw_usage if isinstance(raw_usage, dict) else {},
    service_tier=None,
    parts=tuple(parts),
    stop=stop,
    refusal='\n\n'.join(part.text for part in parts if isinstance(part, Message))
    if reason == 'refusal'
    else None,
    stop_reason=reason if isinstance(reason, str) else None,
  )


def counts(usage: dict) -> dict[str, int]:
  return {
    'input': int(usage.get('input_tokens', 0)),
    'cache_write': int(usage.get('cache_creation_input_tokens', 0)),
    'cache_read': int(usage.get('cache_read_input_tokens', 0)),
    'output': int(usage.get('output_tokens', 0)),
  }
