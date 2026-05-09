#!/usr/bin/env python
"""sync Claude Code session logs to S3 + DynamoDB."""

import datetime
import json
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

import boto3

from base import log
from base.args import Parser

_CONFIG_PATH = Path(__file__).resolve().parent / '.configs' / 'session_log.json'

__cli_name__ = 'sync-session-log'


def _load_config() -> dict:
  with open(_CONFIG_PATH) as f:
    return json.load(f)


def _create_session(config: dict) -> boto3.Session:
  return boto3.Session(
    aws_access_key_id=config['aws_access_key_id'],
    aws_secret_access_key=config['aws_secret_access_key'],
    region_name=config['region'],
  )


def _encode_cwd(cwd: str) -> str:
  return cwd.replace('/', '-').replace('.', '-')


def _projects_dir() -> Path:
  projects_root = Path.home() / '.claude' / 'projects'
  pwd = os.environ.get('PWD')
  cwd = Path(pwd if pwd is not None else os.getcwd()).resolve()
  for candidate in [cwd, *cwd.parents]:
    project_dir = projects_root / _encode_cwd(str(candidate))
    if project_dir.is_dir():
      return project_dir
  return projects_root / _encode_cwd(str(cwd))


def _latest_jsonl(projects_dir: Path) -> Path | None:
  if not projects_dir.is_dir():
    return None
  jsonls = [p for p in projects_dir.iterdir() if p.suffix == '.jsonl']
  if len(jsonls) == 0:
    return None
  return max(jsonls, key=lambda p: p.stat().st_mtime)


def _extract_metadata(path: Path) -> dict:
  subject: str | None = None
  model: str | None = None
  first_ts: str | None = None
  line_count = 0

  with path.open() as f:
    for line in f:
      line_count += 1
      try:
        entry = json.loads(line)
      except json.JSONDecodeError:
        continue

      if first_ts is None:
        ts = entry.get('timestamp')
        if isinstance(ts, str):
          first_ts = ts

      if subject is None and entry.get('type') == 'user' and entry.get('isSidechain') is not True:
        content = entry.get('message', {}).get('content')
        text: str | None = None
        if isinstance(content, str):
          text = content
        elif isinstance(content, list):
          for c in content:
            if isinstance(c, dict) and c.get('type') == 'text':
              text = c.get('text')
              break
        if text is not None:
          stripped = text.lstrip()
          if not stripped.startswith('<'):
            first_line = stripped.split('\n', 1)[0].strip()
            if len(first_line) > 0:
              subject = first_line

      msg = entry.get('message')
      if isinstance(msg, dict) and 'model' in msg:
        model = msg['model']

  return {
    'subject': subject,
    'model': model,
    'started_at': first_ts,
    'line_count': line_count,
  }


def _workspace_name() -> str | None:
  name = os.environ.get('CW_NAME')
  if name is not None:
    return name
  cw_cmd = os.environ.get('CW_COMMAND')
  if cw_cmd is None:
    return None
  parts = cw_cmd.split()
  if len(parts) < 3 or parts[0] != 'cw' or parts[1] != 'ss':
    return None
  i = 2
  while i < len(parts):
    arg = parts[i]
    if not arg.startswith('-'):
      return arg
    if '=' in arg:
      i += 1
      continue
    if arg == '--remote-control':
      i += 2
      continue
    i += 1
  return None


def _to_attr(value: str | int | bool) -> dict:
  if isinstance(value, bool):
    return {'BOOL': value}
  if isinstance(value, int):
    return {'N': str(value)}
  return {'S': str(value)}


def _build_item(path: Path, workspace: str, s3_key: str) -> dict:
  meta = _extract_metadata(path)
  stat = path.stat()

  item: dict = {
    'session_id': path.stem,
    'workspace': workspace,
    'host': socket.gethostname(),
    's3_key': s3_key,
    'size_bytes': int(stat.st_size),
    'line_count': meta['line_count'],
    'synced_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'is_container': os.path.isfile('/.dockerenv'),
  }

  for key, env in [('cw_command', 'CW_COMMAND'), ('ppp_command', 'PPP_COMMAND')]:
    val = os.environ.get(env)
    if val is not None:
      item[key] = val

  for key in ('subject', 'model', 'started_at'):
    if meta[key] is not None:
      item[key] = meta[key]

  return item


def _sync_once(path: Path, workspace: str, bucket: str, s3, dynamo, table_name: str) -> None:
  s3_key = f'logs/{workspace}/{path.stem}.jsonl'
  s3.upload_file(str(path), bucket, s3_key)
  item = _build_item(path, workspace, s3_key)
  dynamo.put_item(TableName=table_name, Item={k: _to_attr(v) for k, v in item.items()})
  log.info('synced %s (%d bytes, %d lines)', path.stem[:12], item['size_bytes'], item['line_count'])


def _watch(interval: int, workspace: str, bucket: str, s3, dynamo, table_name: str) -> None:
  projects_dir = _projects_dir()
  last_mtime = 0.0
  last_path: Path | None = None
  stop = threading.Event()

  def _handle_signal(signum, frame):
    stop.set()

  signal.signal(signal.SIGTERM, _handle_signal)
  signal.signal(signal.SIGINT, _handle_signal)

  while not stop.is_set():
    path = _latest_jsonl(projects_dir)
    if path is not None:
      try:
        mtime = path.stat().st_mtime
      except OSError:
        mtime = last_mtime
      if path != last_path or mtime != last_mtime:
        try:
          _sync_once(path, workspace, bucket, s3, dynamo, table_name)
          last_mtime = mtime
          last_path = path
        except Exception:
          log.exception('sync failed')
    stop.wait(interval)

  path = _latest_jsonl(projects_dir)
  if path is not None:
    try:
      _sync_once(path, workspace, bucket, s3, dynamo, table_name)
    except Exception:
      log.exception('final sync failed')


def sync_session_log(
  watch: bool = False,
  interval: int = 60,
  workspace: str | None = None,
) -> int:
  ws = workspace if workspace is not None else _workspace_name()
  if ws is None:
    log.error('cannot determine workspace name; pass --workspace or set CW_COMMAND/CW_NAME')
    return 1

  if not _CONFIG_PATH.is_file():
    log.error('config not found: %s (run setup/bootstrap_session_log.sh)', _CONFIG_PATH)
    return 1

  config = _load_config()
  session = _create_session(config)
  bucket = config['bucket']
  table_name = config['table']
  s3 = session.client('s3')
  dynamo = session.client('dynamodb')

  if watch:
    log.info('watching for changes (interval=%ds, workspace=%s)', interval, ws)
    _watch(interval, ws, bucket, s3, dynamo, table_name)
    return 0

  projects_dir = _projects_dir()
  path = _latest_jsonl(projects_dir)
  if path is None:
    log.error('no session log found in %s', projects_dir)
    return 1

  _sync_once(path, ws, bucket, s3, dynamo, table_name)
  return 0


def main(argv=None):
  parser = Parser(description='sync Claude Code session logs to S3 + DynamoDB')
  parser.add_argument('--watch', action='store_true', help='poll for changes and sync continuously')
  parser.add_argument(
    '--interval', type=int, default=1, help='poll interval in seconds (default: 1)'
  )
  parser.add_argument(
    '--workspace', default=None, help='workspace name (default: from CW_COMMAND/CW_NAME)'
  )
  return sync_session_log(**parser.parse(argv))


if __name__ == '__main__':
  sys.exit(main(sys.argv))
