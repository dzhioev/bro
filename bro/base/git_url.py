"""git remote URL grammar: recognizing one and reducing it to a canonical
spelling, so the same remote written two ways compares equal."""

import re
import urllib.parse

_SCHEME_URL = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*://')
_SCP_URL = re.compile(r'^(?:[^/@:\s]+@)?[^/:\s]+:.+$')


def is_git_url(value: str) -> bool:
  return _SCHEME_URL.match(value) is not None or _SCP_URL.match(value) is not None


def normalize_git_url(value: str) -> str:
  """the canonical spelling of a git URL; raises when the value is not one."""
  if _SCHEME_URL.match(value) is not None:
    parsed = urllib.parse.urlsplit(value)
    if parsed.hostname is None:
      if parsed.scheme.lower() != 'file' or parsed.netloc:
        raise ValueError(f'malformed git URL: {value!r}')
      netloc = ''
    else:
      hostname = parsed.hostname.lower()
      if ':' in hostname and not hostname.startswith('['):
        hostname = f'[{hostname}]'
      user_info = parsed.netloc.rsplit('@', 1)[0] + '@' if '@' in parsed.netloc else ''
      port = '' if parsed.port is None else f':{parsed.port}'
      netloc = f'{user_info}{hostname}{port}'
    path = parsed.path.rstrip('/') or '/'
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ''))
  if _SCP_URL.match(value) is not None:
    host, path = value.split(':', 1)
    if '@' in host:
      user, hostname = host.rsplit('@', 1)
      host = f'{user}@{hostname.lower()}'
    else:
      host = host.lower()
    return f'{host}:{path.rstrip("/")}'
  raise ValueError(f'not a git URL: {value!r}')


def git_url_path(normalized: str) -> str:
  """the repository path of a `normalize_git_url` result."""
  if _SCHEME_URL.match(normalized) is not None:
    return urllib.parse.urlsplit(normalized).path
  return normalized.split(':', 1)[-1]
