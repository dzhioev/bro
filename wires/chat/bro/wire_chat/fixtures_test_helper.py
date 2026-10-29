"""Fixtures from https://api-docs.deepseek.com/api/create-chat-completion (not live captures)."""


def response(
  content: str | None = 'reply',
  *,
  reasoning: str | None = None,
  calls=None,
  stop: str = 'stop',
  **fields,
) -> dict:
  return {
    'id': 'chat_fixture',
    'object': 'chat.completion',
    'created': 0,
    'model': 'deepseek-v4-pro',
    'choices': [
      {
        'index': 0,
        'finish_reason': stop,
        'message': {
          'role': 'assistant',
          'content': content,
          **({'reasoning_content': reasoning} if reasoning is not None else {}),
          **({'tool_calls': calls} if calls is not None else {}),
        },
      }
    ],
    'usage': {
      'prompt_tokens': 10,
      'prompt_cache_hit_tokens': 4,
      'prompt_cache_miss_tokens': 6,
      'completion_tokens': 3,
      'completion_tokens_details': {'reasoning_tokens': 2},
      'total_tokens': 13,
    },
    **fields,
  }


def call(arguments: str = '{}', name: str = 'svc__ping', call_id: str = 'call_1') -> dict:
  return {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}
