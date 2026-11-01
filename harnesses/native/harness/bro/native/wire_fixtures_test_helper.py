"""Canned protocol replies and expected request items for the wire conformance suite."""

from dataclasses import dataclass

from bro.deepseek.declaration import Recipe as ChatSpec
from bro.llm.llm import Recipe
from bro.llm.records import Call, Message, Reasoning
from bro.native.echo_declaration import Recipe as EchoSpec
from bro.openai.declaration import Recipe as ResponsesSpec
from bro.wire_chat.fixtures_test_helper import call as chat_call, response as chat_response


@dataclass(frozen=True)
class Case:
  recipe: Recipe
  response: dict
  parts: tuple
  stop: str = 'completed'
  counts: dict | None = None


def message(text: str) -> dict:
  return {
    'type': 'message',
    'role': 'assistant',
    'id': 'message_1',
    'status': 'completed',
    'content': [{'type': 'output_text', 'text': text, 'annotations': []}],
  }


def response(*output: dict, **fields) -> dict:
  return {
    'id': 'response_1',
    'object': 'response',
    'created_at': 0,
    'model': 'gpt-6.1-sol',
    'status': 'completed',
    'output': list(output),
    'usage': None,
    **fields,
  }


def call(arguments: str, name: str = 'svc__ping') -> dict:
  return {
    'type': 'function_call',
    'id': 'function_1',
    'call_id': 'call_1',
    'name': name,
    'arguments': arguments,
    'status': 'completed',
  }


def cases() -> dict[str, dict[str, Case]]:
  recipe = ResponsesSpec(model='gpt-6.1-sol')
  cases = {
    'plain': Case(recipe, response(message('reply')), (Message(1, 'reply'),)),
    'reasoning': Case(
      recipe,
      response(
        {
          'type': 'reasoning',
          'id': 'reasoning_1',
          'summary': [{'type': 'summary_text', 'text': 'thought'}],
        },
        message('reply'),
      ),
      (Reasoning(1, 'thought'), Message(2, 'reply')),
    ),
    'silent': Case(recipe, response({**message(''), 'phase': 'final_answer'}), ()),
  }
  for name, arguments in [('valid', '{"x":1}'), ('empty', ''), ('malformed', '{')]:
    cases[name] = Case(
      recipe,
      response(message('checking'), call(arguments)),
      (Message(1, 'checking'), Call.parse(2, 'call_1', 'svc__ping', arguments)),
    )
  cases['usage'] = Case(
    recipe,
    response(
      message('reply'),
      usage={
        'input_tokens': 10,
        'input_tokens_details': {'cached_tokens': 4, 'cache_write_tokens': 1},
        'output_tokens': 3,
        'output_tokens_details': {'reasoning_tokens': 2},
        'total_tokens': 13,
      },
    ),
    (Message(1, 'reply'),),
    counts={'input': 5, 'cache_read': 4, 'cache_write': 1, 'output': 3},
  )
  for name, status, reason in [
    ('truncated', 'incomplete', 'max_output_tokens'),
    ('filtered', 'incomplete', 'content_filter'),
    ('failed', 'failed', None),
  ]:
    cases[name] = Case(
      recipe,
      response(
        message('partial'), status=status, incomplete_details={'reason': reason} if reason else None
      ),
      (Message(1, 'partial'),),
      stop=name,
    )
  return {
    'responses': cases,
    'chat': chat_cases(),
    'echo': {'plain': Case(EchoSpec(), {'model': 'echo', 'text': 'reply'}, (Message(1, 'reply'),))},
  }


RESTORE_ITEMS = {
  'responses': {
    'field': 'input',
    'initial': [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'reply'}],
    'assistant': [{key: value for key, value in message('reply').items() if key != 'status'}],
    'next': [{'role': 'user', 'content': 'next'}],
  },
  'echo': {
    'field': 'input',
    'initial': [{'role': 'system', 'text': 'system'}, {'role': 'user', 'text': 'reply'}],
    'assistant': [{'role': 'assistant', 'text': 'reply'}],
    'next': [{'role': 'user', 'text': 'next'}],
  },
}


def capture_requests(monkeypatch, sent: list[dict]) -> None:
  from bro.native.echo import Echo

  respond = Echo._respond

  def captured(self, request):
    sent.append(request)
    return respond(self, request)

  monkeypatch.setattr(Echo, '_respond', captured)


def chat_cases() -> dict[str, Case]:
  recipe = ChatSpec()
  usage = {'input': 6, 'cache_read': 4, 'cache_write': 0, 'output': 3}
  cases = {
    'plain': Case(recipe, chat_response(), (Message(1, 'reply'),), counts=usage),
    'reasoning': Case(
      recipe,
      chat_response(reasoning='thought'),
      (Reasoning(1, 'thought'), Message(2, 'reply')),
      counts=usage,
    ),
    'silent': Case(recipe, chat_response(None), (), counts=usage),
  }
  for name, arguments in [('valid', '{"x":1}'), ('empty', ''), ('malformed', '{')]:
    cases[name] = Case(
      recipe,
      chat_response('checking', calls=[chat_call(arguments)], stop='tool_calls'),
      (Message(1, 'checking'), Call.parse(2, 'call_1', 'svc__ping', arguments)),
      counts=usage,
    )
  for reason, stop in [
    ('length', 'truncated'),
    ('content_filter', 'filtered'),
    ('insufficient_system_resource', 'capacity'),
    ('aborted', 'failed'),
  ]:
    cases[stop] = Case(
      recipe,
      chat_response('partial', stop=reason),
      (Message(1, 'partial'),),
      stop=stop,
      counts=usage,
    )
  return cases


RESTORE_ITEMS['chat'] = {
  'field': 'messages',
  'initial': [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'reply'}],
  'assistant': [{'role': 'assistant', 'content': 'reply'}],
  'next': [{'role': 'user', 'content': 'next'}],
}
