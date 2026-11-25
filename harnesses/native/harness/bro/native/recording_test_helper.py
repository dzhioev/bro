from bro.native.runner import Runner
from bro.native.trail_recorder import Recorder
from bro.trails.record.recording_map import RecordingMap
from bro.trails.record.session import ManagedSession
from bro.trails.store import TrailsStore


def record_run(store: TrailsStore, runner: Runner) -> str:
  assert runner.run_log is not None
  recorder = Recorder(
    runner.run_log,
    store,
    recording_map=RecordingMap.load(runner.run_log.parent),
    session=ManagedSession(
      'test', 'host', str(runner.run_log.parent), False, 'test', None, None, None, None
    ),
    log_path=runner.run_log.with_suffix('.recorder.log'),
  )
  assert recorder.tick(final=True)
  assert recorder.recording is not None
  return recorder.recording.trail_id
