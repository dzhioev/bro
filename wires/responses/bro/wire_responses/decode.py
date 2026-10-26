"""Responses API records without the SDK."""

from bro.llm.records import Call, DecodedCall, Message, Part, Reasoning, Stop, Unknown

NAME = 'responses'
CHAINING = 'server'
LEGACY = True


def decode(response: dict) -> DecodedCall:
  parts: list[Part] = []
  refusals: list[str] = []
  output = response.get('output')
  if not isinstance(output, list):
    parts.append(Unknown(1, response))
  else:
    for index, item in enumerate(output, start=1):
      if not isinstance(item, dict):
        parts.append(Unknown(index, item))
      elif item.get('type') == 'reasoning':
        summary = item.get('summary')
        if not isinstance(summary, list):
          parts.append(Unknown(index, item))
          continue
        for part in summary:
          if isinstance(part, dict) and part.get('type') == 'summary_text':
            text = part.get('text')
            if isinstance(text, str) and text:
              parts.append(Reasoning(index, text))
      elif item.get('type') == 'message':
        content = item.get('content')
        if not isinstance(content, list):
          parts.append(Unknown(index, item))
          continue
        texts: list[str] = []
        for part in content:
          if not isinstance(part, dict):
            continue
          if part.get('type') == 'output_text' and isinstance(part.get('text'), str):
            texts.append(part['text'])
          elif part.get('type') == 'refusal' and isinstance(part.get('refusal'), str):
            refusals.append(part['refusal'])
        text = ''.join(texts)
        if text:
          parts.append(Message(index, text, commentary=item.get('phase') == 'commentary'))
      elif item.get('type') == 'function_call':
        parts.append(
          Call.parse(index, item.get('call_id'), item.get('name'), item.get('arguments'))
        )
      else:
        parts.append(Unknown(index, item))
  status = response.get('status')
  details = response.get('incomplete_details')
  reason = details.get('reason') if isinstance(details, dict) else None
  stop: Stop = 'failed'
  if status == 'completed':
    stop = 'completed'
  elif status == 'incomplete':
    if reason == 'max_output_tokens':
      stop = 'truncated'
    elif reason == 'content_filter':
      stop = 'filtered'
  raw_usage = response.get('usage')
  tier = response.get('service_tier')
  return DecodedCall(
    model=str(response.get('model', 'unknown')),
    usage=raw_usage if isinstance(raw_usage, dict) else {},
    service_tier=tier if isinstance(tier, str) else None,
    parts=tuple(parts),
    stop=stop,
    refusal='\n\n'.join(refusals) if refusals else None,
    stop_reason=reason if isinstance(reason, str) else status if isinstance(status, str) else None,
  )


def counts(usage: dict) -> dict[str, int]:
  details = usage.get('input_tokens_details')
  if details is None:
    cache_read = cache_write = 0
    input_tokens = int(usage.get('input_tokens', 0))
  else:
    cache_read = int(details.get('cached_tokens', 0))
    cache_write = int(details.get('cache_write_tokens', 0))
    input_tokens = int(usage['input_tokens'])
  return {
    'input': input_tokens - cache_read - cache_write,
    'cache_write': cache_write,
    'cache_read': cache_read,
    'output': int(usage.get('output_tokens', 0)),
  }
