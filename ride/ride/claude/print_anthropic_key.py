"""claude's apiKeyHelper for `--raw` sessions: prints the `anthropic` api key.

A leaf wrapper around the resolver rather than the `credentials` CLI itself:
`bro.base.credentials` is imported too widely to be named to `python -m`.
"""

import sys
from typing import Optional

from ride.claude.claude_auth import _load_anthropic_key


def main(argv: list[str]) -> Optional[int]:
  del argv
  key = _load_anthropic_key()
  if key is None:
    print('the `anthropic` secret resolves no api_key', file=sys.stderr)
    return 1
  print(key)
  return None


if __name__ == '__main__':
  sys.exit(main(sys.argv))
