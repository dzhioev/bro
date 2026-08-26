"""Convert benchmark trails into Harbor's ATIF trajectory model."""

import json
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from harbor.models.job.result import JobResult
from harbor.models.trajectories import (
  Agent,
  FinalMetrics,
  Metrics,
  Observation,
  ObservationResult,
  Step,
  SubagentTrajectoryRef,
  ToolCall,
  Trajectory,
)

from bro.benchmark.harbor_agent import reported_agent_name, reported_agent_version
from bro.benchmark.pricing import call_cost_usd, optional_call_cost_usd
from bro.llm.usage import Counts, from_vendor_counts
from bro.trails.local import LocalStore

# harbor runs a trial's bro with the trial's `agent/` directory as its data
# home, so the store the run recorded into is the local backend's root beneath
# it (`bro.trails.store.local_root`)
TRAILS_DIRECTORY = Path('ride') / 'trails'
TRAJECTORY_FILENAME = 'trajectory.json'


@dataclass
class _ConvertedSteps:
  steps: list[Step]
  calls_by_source: dict[tuple[int, int], str]
  results_by_call: dict[str, ObservationResult]
  cost_usd: Decimal | None


@dataclass
class _BuiltTrajectory:
  trajectory: Trajectory
  cost_usd: Decimal | None


def _required_string(value: Any, field: str) -> str:
  if not isinstance(value, str) or len(value) == 0:
    raise ValueError(f'{field} must be a non-empty string')
  return value


def _nonnegative_integer(value: Any, field: str) -> int:
  if not isinstance(value, int) or isinstance(value, bool) or value < 0:
    raise ValueError(f'{field} must be a non-negative int')
  return value


def _source(message: dict[str, Any]) -> tuple[int, int]:
  source = message.get('source')
  if not isinstance(source, dict):
    raise ValueError('projected message source must be an object')
  return (
    _nonnegative_integer(source.get('step_id'), 'projected message source.step_id'),
    _nonnegative_integer(source.get('index'), 'projected message source.index'),
  )


def _content(value: Any) -> str | None:
  if value is None or isinstance(value, str):
    return value
  return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _metrics(model: str, raw: Any) -> tuple[Metrics, Decimal | None]:
  if not isinstance(raw, dict):
    raise ValueError('llm_call usage must be an object')
  counts = from_vendor_counts(raw)
  cost = optional_call_cost_usd(model, counts)
  return (
    Metrics(
      prompt_tokens=counts['input'] + counts['cache_write'] + counts['cache_read'],
      completion_tokens=counts['output'],
      cached_tokens=counts['cache_read'],
      cost_usd=None if cost is None else float(cost),
      extra={'usage': counts},
    ),
    cost,
  )


def _standalone_step(step_id: int, message: dict[str, Any]) -> Step:
  message_type = message.get('type')
  timestamp = message.get('ts')
  if timestamp is not None and not isinstance(timestamp, str):
    raise ValueError('projected message ts must be a string or null')
  if message_type == 'system_prompt':
    source = 'system'
    content = message.get('content')
  elif message_type == 'user_input':
    source = 'user'
    content = message.get('content')
  elif message_type == 'error':
    source = 'system'
    content = message.get('content')
  else:
    raise ValueError(f'unsupported standalone projected message type: {message_type!r}')
  rendered = _content(content)
  if rendered is None:
    raise ValueError(f'{message_type} content is required')
  return Step(step_id=step_id, timestamp=timestamp, source=source, message=rendered)


def _agent_step(
  step_id: int,
  model: str,
  messages: list[dict[str, Any]],
  tool_results: dict[str, dict[str, Any]],
) -> tuple[Step, dict[tuple[int, int], str], dict[str, ObservationResult], Decimal | None]:
  llm_calls = [message for message in messages if message.get('type') == 'llm_call']
  if len(llm_calls) != 1:
    raise ValueError(
      f'an llm_call source group must hold exactly one llm_call, got {len(llm_calls)}'
    )
  llm_call = llm_calls[0]
  raw_step_id, _ = _source(llm_call)
  assistant_messages = [message for message in messages if message.get('type') == 'assistant']
  reasoning_messages = [message for message in messages if message.get('type') == 'reasoning']
  tool_messages = [message for message in messages if message.get('type') == 'tool_call']
  supported = {'llm_call', 'assistant', 'reasoning', 'tool_call'}
  unsupported = {str(message.get('type')) for message in messages} - supported
  if len(unsupported) > 0:
    raise ValueError(f'unsupported llm_call projected message types: {sorted(unsupported)}')

  assistant_content = [
    _required_string(message.get('content'), 'assistant content') for message in assistant_messages
  ]
  reasoning_content = [
    _required_string(message.get('content'), 'reasoning content') for message in reasoning_messages
  ]
  calls: list[ToolCall] = []
  calls_by_source: dict[tuple[int, int], str] = {}
  observations: list[ObservationResult] = []
  results_by_call: dict[str, ObservationResult] = {}
  for message in tool_messages:
    call_id = _required_string(message.get('call_id'), 'tool_call call_id')
    position = _source(message)
    if position in calls_by_source:
      raise ValueError(f'multiple tool calls occupy projected source {position}')
    arguments = message.get('arguments')
    if not isinstance(arguments, dict):
      raise ValueError(f'tool_call {call_id} arguments must be an object')
    calls_by_source[position] = call_id
    calls.append(
      ToolCall(
        tool_call_id=call_id,
        function_name=_required_string(message.get('tool_name'), 'tool_call tool_name'),
        arguments=arguments,
      )
    )
    result_message = tool_results.pop(call_id, None)
    if result_message is None:
      continue
    extra = {'is_error': True} if result_message.get('is_error') is True else None
    result = ObservationResult(
      source_call_id=call_id,
      content=_content(result_message.get('content')),
      extra=extra,
    )
    observations.append(result)
    results_by_call[call_id] = result

  timestamp = llm_call.get('ts')
  if timestamp is not None and not isinstance(timestamp, str):
    raise ValueError('llm_call ts must be a string or null')
  metrics, cost = _metrics(model, llm_call.get('usage'))
  step = Step(
    step_id=step_id,
    timestamp=timestamp,
    source='agent',
    message='\n\n'.join(assistant_content),
    reasoning_content='\n\n'.join(reasoning_content) if len(reasoning_content) > 0 else None,
    tool_calls=calls or None,
    observation=Observation(results=observations) if len(observations) > 0 else None,
    metrics=metrics,
    llm_call_count=1,
  )
  if any(source_step_id != raw_step_id for source_step_id, _ in calls_by_source):
    raise ValueError('tool_call projected outside its llm_call source group')
  return step, calls_by_source, results_by_call, cost


def _convert_steps(model: str, messages: list[dict[str, Any]]) -> _ConvertedSteps:
  groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
  tool_results: dict[str, dict[str, Any]] = {}
  for message in messages:
    source_step_id, _ = _source(message)
    if message.get('type') == 'tool_result':
      call_id = _required_string(message.get('call_id'), 'tool_result call_id')
      if call_id in tool_results:
        raise ValueError(f'multiple tool results answer call {call_id}')
      tool_results[call_id] = message
    else:
      groups[source_step_id].append(message)

  steps: list[Step] = []
  calls_by_source: dict[tuple[int, int], str] = {}
  results_by_call: dict[str, ObservationResult] = {}
  total_cost: Decimal | None = Decimal(0)
  for raw_step_id in sorted(groups):
    group = groups[raw_step_id]
    if any(message.get('type') == 'llm_call' for message in group):
      step, group_calls, group_results, cost = _agent_step(
        len(steps) + 1, model, group, tool_results
      )
      overlapping_calls = set(calls_by_source) & set(group_calls)
      if len(overlapping_calls) > 0:
        raise ValueError(f'duplicate projected tool-call sources: {sorted(overlapping_calls)}')
      calls_by_source.update(group_calls)
      results_by_call.update(group_results)
      if cost is None:
        total_cost = None
      elif total_cost is not None:
        total_cost += cost
      steps.append(step)
      continue
    if len(group) != 1:
      raise ValueError(f'projected source step {raw_step_id} does not form one ATIF step')
    steps.append(_standalone_step(len(steps) + 1, group[0]))

  if len(tool_results) > 0:
    raise ValueError(f'tool results have no projected calls: {sorted(tool_results)}')
  return _ConvertedSteps(steps, calls_by_source, results_by_call, total_cost)


def _model_name(header: dict[str, Any]) -> str:
  native = header.get('native')
  if not isinstance(native, dict):
    raise ValueError('trail header native must be an object')
  llm = native.get('llm')
  if not isinstance(llm, dict):
    raise ValueError('trail header native.llm must be an object')
  return _required_string(llm.get('model'), 'trail header native.llm.model')


def _summoner(header: dict[str, Any]) -> tuple[str, int, int] | None:
  pointer = header.get('summoned_by')
  if pointer is None:
    return None
  if not isinstance(pointer, dict):
    raise ValueError('trail header summoned_by must be an object')
  trail_id = _required_string(pointer.get('trail_id'), 'trail header summoned_by.trail_id')
  return (
    trail_id,
    _nonnegative_integer(pointer.get('step_id'), 'trail header summoned_by.step_id'),
    _nonnegative_integer(pointer.get('index'), 'trail header summoned_by.index'),
  )


def _build_trajectory(
  trail_id: str,
  headers: dict[str, dict[str, Any]],
  messages: dict[str, list[dict[str, Any]]],
  children: dict[str, list[str]],
  agent_version: str,
  visiting: set[str],
  visited: set[str],
) -> _BuiltTrajectory:
  if trail_id in visiting:
    raise ValueError(f'summoned trail cycle reaches {trail_id}')
  visiting.add(trail_id)
  header = headers[trail_id]
  model = _model_name(header)
  converted = _convert_steps(model, messages[trail_id])
  total_cost = converted.cost_usd
  subagents: list[Trajectory] = []
  for child_id in children.get(trail_id, []):
    pointer = _summoner(headers[child_id])
    assert pointer is not None
    _, source_step_id, source_index = pointer
    call_id = converted.calls_by_source.get((source_step_id, source_index))
    if call_id is None:
      raise ValueError(
        f'summoned trail {child_id} points to missing tool call {trail_id}:{source_step_id}:{source_index}'
      )
    result = converted.results_by_call.get(call_id)
    if result is None:
      raise ValueError(f'summoned trail {child_id} has no tool result for call {call_id}')
    child = _build_trajectory(
      child_id, headers, messages, children, agent_version, visiting, visited
    )
    result.subagent_trajectory_ref = [
      *(result.subagent_trajectory_ref or []),
      SubagentTrajectoryRef(trajectory_id=child_id, session_id=child_id),
    ]
    if child.cost_usd is None:
      total_cost = None
    elif total_cost is not None:
      total_cost += child.cost_usd
    subagents.append(child.trajectory)

  visiting.remove(trail_id)
  visited.add(trail_id)
  trajectory = Trajectory(
    schema_version='ATIF-v1.7',
    session_id=trail_id,
    trajectory_id=trail_id,
    agent=Agent(
      name=reported_agent_name(_required_string(header.get('bro'), 'trail header bro')),
      version=agent_version,
      model_name=model,
    ),
    steps=converted.steps,
    final_metrics=(None if total_cost is None else FinalMetrics(total_cost_usd=float(total_cost))),
    subagent_trajectories=subagents or None,
  )
  return _BuiltTrajectory(trajectory, total_cost)


def trajectory_from_store(store: LocalStore) -> Trajectory:
  headers = {
    _required_string(header.get('id'), 'trail header id'): header for header in store.iter_trails()
  }
  if len(headers) == 0:
    raise ValueError(f'trails store at {store.root} holds no trails')
  roots = [trail_id for trail_id, header in headers.items() if _summoner(header) is None]
  if len(roots) != 1:
    raise ValueError(f'trails store must hold exactly one root trail, found {len(roots)}')

  children: dict[str, list[str]] = defaultdict(list)
  for trail_id, header in headers.items():
    pointer = _summoner(header)
    if pointer is None:
      continue
    parent_id, _, _ = pointer
    if parent_id not in headers:
      raise ValueError(f'summoned trail {trail_id} names missing parent {parent_id}')
    children[parent_id].append(trail_id)
  for child_ids in children.values():
    child_ids.sort()

  projected = {trail_id: list(store.iter_messages(trail_id)) for trail_id in headers}
  visited: set[str] = set()
  built_trajectory = _build_trajectory(
    roots[0],
    headers,
    projected,
    children,
    reported_agent_version(),
    set(),
    visited,
  )
  disconnected = set(headers) - visited
  if len(disconnected) > 0:
    raise ValueError(f'trails are disconnected from the root: {sorted(disconnected)}')
  return Trajectory.model_validate(
    built_trajectory.trajectory.model_dump(mode='json', exclude_none=True)
  )


def trajectory_cost_usd(trajectory: Trajectory) -> Decimal:
  """Strictly reprice every recorded LLM call in one ATIF trajectory tree."""
  model = _required_string(trajectory.agent.model_name, 'trajectory agent.model_name')
  total = Decimal(0)
  for step in trajectory.steps:
    if step.llm_call_count is None:
      continue
    if step.llm_call_count != 1:
      raise ValueError(
        f'trajectory step {step.step_id} must represent exactly one LLM call for costing'
      )
    if step.metrics is None or not isinstance(step.metrics.extra, dict):
      raise ValueError(f'trajectory step {step.step_id} has no recorded billed-class usage')
    usage = step.metrics.extra.get('usage')
    if not isinstance(usage, dict):
      raise ValueError(f'trajectory step {step.step_id} has no recorded billed-class usage')
    total += call_cost_usd(model, cast(Counts, usage))
  for child in trajectory.subagent_trajectories or []:
    total += trajectory_cost_usd(child)
  return total


def job_trajectory_cost_usd(job_directory: Path) -> Decimal:
  """Strictly report the total cost of every converted trial in a job."""
  total = Decimal(0)
  for path in sorted(job_directory.glob(f'*/agent/{TRAJECTORY_FILENAME}')):
    trajectory = Trajectory.model_validate_json(path.read_text())
    total += trajectory_cost_usd(trajectory)
  return total


def convert_trial_trajectory(agent_directory: Path) -> Path:
  store_root = agent_directory / TRAILS_DIRECTORY
  if not (store_root / 'trails').is_dir():
    raise ValueError(f'trial agent directory holds no trails store: {agent_directory}')
  with LocalStore(store_root) as store:
    trajectory = trajectory_from_store(cast(LocalStore, store))
  destination = agent_directory / TRAJECTORY_FILENAME
  destination.write_text(trajectory.model_dump_json(indent=2, exclude_none=True) + '\n')
  return destination


def _recorded_a_trail(store_root: Path) -> bool:
  """whether the trial's store holds one. Opening a store creates its own
  directories, so a run that ended before blazing a trail leaves the layout
  behind with nothing in it."""
  if not (store_root / 'trails').is_dir():
    return False
  with LocalStore(store_root) as store:
    return next(store.iter_trails(max_items=1), None) is not None


def convert_job_trajectories(job_directory: Path) -> list[Path]:
  result_path = job_directory / 'result.json'
  if not result_path.is_file():
    raise ValueError(f'job directory holds no result.json: {job_directory}')
  result = JobResult.model_validate_json(result_path.read_text())
  if result.finished_at is None:
    raise ValueError(f'job has not finished: {job_directory}')

  destinations: list[Path] = []
  for trial_directory in sorted(job_directory.iterdir()):
    if not trial_directory.is_dir() or not (trial_directory / 'result.json').is_file():
      continue
    agent_directory = trial_directory / 'agent'
    if not _recorded_a_trail(agent_directory / TRAILS_DIRECTORY):
      continue
    destinations.append(convert_trial_trajectory(agent_directory))
  return destinations
