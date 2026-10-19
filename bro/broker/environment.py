"""Launch-time broker names and talk vocabulary."""

from typing import Literal

BROKER_CHANNEL = 'BROKER_CHANNEL'
BROKER_UPSTREAM = 'BROKER_UPSTREAM'
BROKER_MISSION = 'BROKER_MISSION'
BROKER_TALK = 'BROKER_TALK'


type TalkRight = Literal[
  'owner.say',
  'owner.question',
  'worker.say',
  'worker.question',
]
type Talk = frozenset[TalkRight]
TALK_RIGHTS: Talk = frozenset({'owner.say', 'owner.question', 'worker.say', 'worker.question'})
EMPTY_TALK: Talk = frozenset()
