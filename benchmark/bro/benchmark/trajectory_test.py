import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from harbor.models.job.result import JobResult, JobStats
from harbor.models.trajectories import Trajectory
from harbor.utils.trajectory_validator import TrajectoryValidator

import bro.benchmark.trajectory as trajectory_module
from bro.benchmark.pricing import UnpricedModelError
from bro.benchmark.trajectory import (
  convert_job_trajectories,
  convert_trial_trajectory,
  trajectory_cost_usd,
  trajectory_from_store,
)
from bro.trails.local import LocalStore
from bro.trails.model import BlazeRequest
from bro.trails.record.spine import Recording

IDENTITY = f'sha256:{"1" * 64}'
MODEL = 'gpt-5.6-terra'


def _request(*, summoned_by=None, model=MODEL) -> BlazeRequest:
  return BlazeRequest(
    harness='bro',
    bro='dev',
    version='test',
    native={'llm': {'type': 'openai', 'model': model}},
    body={'records': [{'kind': 'system_prompt', 'body': 'You are a bro.'}]},
    interactive=False,
    surface='benchmark',
    summoned_by=summoned_by,
  )


def _llm_call(*output: dict, usage: dict | None = None) -> dict:
  return {
    'kind': 'llm_call',
    'body': {
      'request': {'model': MODEL, 'input': []},
      'response': {
        'id': str(uuid4()),
        'model': MODEL,
        'output': list(output),
        'usage': usage or {'input_tokens': 2, 'output_tokens': 1},
      },
    },
  }


def _tool_call(call_id: str, name: str = 'repo__read') -> dict:
  return {
    'type': 'function_call',
    'call_id': call_id,
    'name': name,
    'arguments': json.dumps({'path': 'README.md'}),
  }


def _assistant(text: str) -> dict:
  return {'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}


def _record_trial(store: LocalStore) -> tuple[str, str]:
  recording = Recording.create(store, _request())
  call_id = 'call-1'
  recording.append(
    [
      {'kind': 'user_input', 'body': 'Read the project.'},
      _llm_call(
        _tool_call(call_id),
        usage={
          'input_tokens': 10,
          'input_tokens_details': {'cached_tokens': 3, 'cache_write_tokens': 2},
          'output_tokens': 4,
        },
      ),
      {
        'kind': 'tool_result',
        'body': {'title': 'Bro'},
        'tool_name': 'repo__read',
        'call_id': call_id,
        'is_error': False,
      },
      _llm_call(
        {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'I read it.'}]},
        _assistant('Done.'),
      ),
    ]
  )
  recording.end('ok')
  return recording.trail_id, call_id


@pytest.fixture(autouse=True)
def _bundle_identity(monkeypatch):
  monkeypatch.setattr(trajectory_module, 'reported_agent_version', lambda: IDENTITY)


def test_a_recorded_trail_round_trips_through_harbors_validator(tmp_path):
  agent_directory = tmp_path / 'agent'
  store = LocalStore(agent_directory / 'ride')
  trail_id, call_id = _record_trial(store)

  destination = convert_trial_trajectory(agent_directory)

  validator = TrajectoryValidator()
  assert validator.validate(destination), validator.get_errors()
  converted = Trajectory.model_validate_json(destination.read_text())
  assert converted.schema_version == 'ATIF-v1.7'
  assert converted.session_id == trail_id
  assert converted.agent.name == 'bro:dev'
  assert converted.agent.version == IDENTITY
  assert converted.agent.model_name == MODEL
  assert [step.source for step in converted.steps] == ['system', 'user', 'agent', 'agent']
  tool_step = converted.steps[2]
  assert tool_step.message == ''
  assert tool_step.tool_calls is not None
  assert tool_step.tool_calls[0].tool_call_id == call_id
  assert tool_step.observation is not None
  assert tool_step.observation.results[0].source_call_id == call_id
  assert tool_step.observation.results[0].content == '{"title": "Bro"}'
  assert tool_step.metrics is not None
  assert tool_step.metrics.prompt_tokens == 10
  assert tool_step.metrics.cached_tokens == 3
  assert tool_step.metrics.completion_tokens == 4
  assert tool_step.metrics.cost_usd == pytest.approx(0.0000636)
  assert tool_step.metrics.extra == {
    'usage': {'input': 5, 'cache_write': 2, 'cache_read': 3, 'output': 4}
  }
  assert converted.steps[3].reasoning_content == 'I read it.'
  assert converted.steps[3].message == 'Done.'
  assert converted.final_metrics is not None
  assert converted.final_metrics.total_cost_usd == pytest.approx(0.0000796)
  assert float(trajectory_cost_usd(converted)) == pytest.approx(0.0000796)


def test_summoned_trails_are_embedded_and_linked_to_their_call(tmp_path):
  store = LocalStore(tmp_path / 'ride')
  parent = Recording.create(store, _request())
  call_id = 'summon-call'
  source_step_id = (
    parent.append(
      [
        {'kind': 'user_input', 'body': 'Delegate.'},
        _llm_call(_tool_call(call_id, 'bro__summon')),
        {
          'kind': 'tool_result',
          'body': 'child answer',
          'tool_name': 'bro__summon',
          'call_id': call_id,
          'is_error': False,
        },
      ]
    )
    + 1
  )
  child = Recording.create(
    store,
    _request(summoned_by={'trail_id': parent.trail_id, 'step_id': source_step_id, 'index': 1}),
  )
  child.append([{'kind': 'user_input', 'body': 'Do it.'}, _llm_call(_assistant('Done.'))])
  child.end('ok')
  parent.end('ok')

  converted = trajectory_from_store(store)

  assert converted.subagent_trajectories is not None
  assert [item.trajectory_id for item in converted.subagent_trajectories] == [child.trail_id]
  observation = converted.steps[2].observation
  assert observation is not None
  references = observation.results[0].subagent_trajectory_ref
  assert references is not None
  assert references[0].trajectory_id == child.trail_id
  assert converted.final_metrics is not None
  assert converted.final_metrics.total_cost_usd == pytest.approx(0.000032)
  assert converted.subagent_trajectories[0].final_metrics is not None
  assert converted.subagent_trajectories[0].final_metrics.total_cost_usd == pytest.approx(0.000016)


def test_an_unpriced_model_keeps_atif_costs_optional_but_fails_a_cost_report(tmp_path):
  store = LocalStore(tmp_path / 'ride')
  recording = Recording.create(store, _request(model='unpriced-model'))
  recording.append([{'kind': 'user_input', 'body': 'Do it.'}, _llm_call(_assistant('Done.'))])
  recording.end('ok')

  converted = trajectory_from_store(store)

  assert converted.steps[2].metrics is not None
  assert converted.steps[2].metrics.cost_usd is None
  assert converted.final_metrics is None
  with pytest.raises(UnpricedModelError, match="no benchmark price for model 'unpriced-model'"):
    trajectory_cost_usd(converted)


def test_multiple_root_trails_are_rejected(tmp_path):
  store = LocalStore(tmp_path / 'ride')
  Recording.create(store, _request())
  Recording.create(store, _request())

  with pytest.raises(ValueError, match='exactly one root trail, found 2'):
    trajectory_from_store(store)


def test_the_job_walker_converts_every_trial_holding_a_trail(tmp_path):
  job_directory = tmp_path / 'job'
  job_directory.mkdir()
  now = datetime.now(UTC)
  result = JobResult(
    id=uuid4(),
    started_at=now,
    finished_at=now,
    n_total_trials=2,
    stats=JobStats(n_completed_trials=2, n_errored_trials=1),
  )
  (job_directory / 'result.json').write_text(result.model_dump_json())
  recorded_trial = job_directory / 'errored-with-trail'
  recorded_trial.mkdir()
  (recorded_trial / 'result.json').write_text('{}')
  store = LocalStore(recorded_trial / 'agent' / 'ride')
  _record_trial(store)
  no_trail = job_directory / 'failed-before-agent'
  no_trail.mkdir()
  (no_trail / 'result.json').write_text('{}')

  destinations = convert_job_trajectories(job_directory)

  assert destinations == [recorded_trial / 'agent' / 'trajectory.json']
  validator = TrajectoryValidator()
  assert validator.validate(destinations[0]), validator.get_errors()
  assert not (no_trail / 'agent' / 'trajectory.json').exists()
