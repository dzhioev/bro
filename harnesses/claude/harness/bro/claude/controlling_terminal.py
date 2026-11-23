"""exec a command with this process's stdin as its controlling terminal.

The process must lead a session that has no controlling terminal yet, as
`subprocess.Popen(..., start_new_session=True)` starts it.
"""

import fcntl
import os
import sys
import termios


def main(argv: list[str]) -> None:
  if len(argv) < 2:
    raise ValueError(f'usage: {argv[0]} <executable> [<argument>...]')
  fcntl.ioctl(0, termios.TIOCSCTTY, 0)
  os.execv(argv[1], argv[1:])


if __name__ == '__main__':
  main(sys.argv)
