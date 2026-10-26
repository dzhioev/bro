"""Chat Completions records without the SDK."""

from bro.llm.records import Call, DecodedCall, Message, Part, Reasoning, Stop, Unknown

NAME = 'chat'
CHAINING = 'client'


_STOPS: dict[str, Stop] = {
  'stop': 'completed',
  'tool_calls': 'completed',
  'length': 'truncated',
  'content_filter': 'filtered',
  'insufficient_system_resource': 'capacity',
}


def decode(response: dict) -> DecodedCall:
  parts: list[Part] = []
  choices = response.get('choices')
  reason = None
  refusal = None
  if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
    parts.append(Unknown(1, response))
  else:
    choice = choices[0]
    reason = choice.get('finish_reason')
    message = choice.get('message')
    if not isinstance(message, dict):
      parts.append(Unknown(1, choice))
    else:
      reasoning = message.get('reasoning_content')
      text = message.get('content')
      if isinstance(reasoning, str) and reasoning:
        parts.append(Reasoning(len(parts) + 1, reasoning))
      if isinstance(text, str) and text:
        parts.append(Message(len(parts) + 1, text))
      refusal = message.get('refusal')
      calls = message.get('tool_calls')
      if calls is not None:
        if not isinstance(calls, list):
          parts.append(Unknown(len(parts) + 1, calls))
        else:
          for call in calls:
            index = len(parts) + 1
            function = call.get('function') if isinstance(call, dict) else None
            if not isinstance(function, dict) or call.get('type') != 'function':
              parts.append(Unknown(index, call))
            else:
              parts.append(
                Call.parse(index, call.get('id'), function.get('name'), function.get('arguments'))
              )
      if not parts:
        parts.append(Unknown(1, message))
  stop = _STOPS.get(reason, 'failed') if isinstance(reason, str) else 'failed'
  raw_usage = response.get('usage')
  return DecodedCall(
    model=str(response.get('model', 'unknown')),
    usage=raw_usage if isinstance(raw_usage, dict) else {},
    service_tier=None,
    parts=tuple(parts),
    stop=stop,
    refusal=refusal if isinstance(refusal, str) else None,
    stop_reason=reason if isinstance(reason, str) else None,
  )


def counts(usage: dict) -> dict[str, int]:
  if 'prompt_cache_hit_tokens' in usage:
    cache_read = int(usage['prompt_cache_hit_tokens'])
  else:
    details = usage.get('prompt_tokens_details')
    cache_read = int(details.get('cached_tokens', 0)) if isinstance(details, dict) else 0
  return {
    'input': int(usage.get('prompt_tokens', 0)) - cache_read,
    'cache_write': 0,
    'cache_read': cache_read,
    'output': int(usage.get('completion_tokens', 0)),
  }
