"""Launch-time broker names and talk vocabulary, and whether a launch intended a channel."""

import os
from typing import Literal

BROKER_CHANNEL = 'BROKER_CHANNEL'
BROKER_UPSTREAM = 'BROKER_UPSTREAM'
BROKER_MISSION = 'BROKER_MISSION'
BROKER_TALK = 'BROKER_TALK'


def channel_intended() -> bool:
  """whether the launch gave this process a broker channel, even one whose proxy failed to start."""
  return any(os.environ.get(name) is not None for name in (BROKER_CHANNEL, BROKER_UPSTREAM))


type TalkRight = Literal[
  'owner.say',
  'owner.question',
  'worker.say',
  'worker.question',
]
type Talk = frozenset[TalkRight]
TALK_RIGHTS: Talk = frozenset({'owner.say', 'owner.question', 'worker.say', 'worker.question'})
EMPTY_TALK: Talk = frozenset()
