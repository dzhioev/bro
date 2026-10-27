"""Canned protocol replies and expected request items for the wire conformance suite."""

from dataclasses import dataclass

from bro.llm.llm import LLMSpec
from bro.llm.llms.echo import LLMSpec as EchoSpec
from bro.llm.llms.openai import LLMSpec as ResponsesSpec
from bro.llm.records import Call, Message, Reasoning


@dataclass(frozen=True)
class Case:
  recipe: LLMSpec
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
    'model': 'fixture-model',
    'status': 'completed',
    'output': list(output),
    'usage': None,
    **fields,
  }


def call(arguments: str) -> dict:
  return {
    'type': 'function_call',
    'id': 'function_1',
    'call_id': 'call_1',
    'name': 'svc__ping',
    'arguments': arguments,
    'status': 'completed',
  }


def cases() -> dict[str, dict[str, Case]]:
  recipe = ResponsesSpec(model='fixture-model')
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
