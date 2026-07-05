"""lulids — lowercase ULIDs, the repo's id mint.

A lulid is a standard ULID restyled for human eyes: lowercase, dash-grouped
10-8-8.

  01kwphn3q5-w1fdwep2-apw9ag3b

The first group is the ULID's 48-bit millisecond timestamp — the part a human
actually compares, since ids minted near each other share it. The two 8-char
groups split the 80-bit entropy tail into scannable halves.

The restyle preserves lexicographic order: Crockford base32 sorts digits before
letters in lowercase just as in uppercase, and the dashes sit at fixed
positions in every id. Lulids therefore sort by mint time like the ULIDs
underneath, so they are safe as range/sort keys.
"""

from ulid import ULID


def lulid() -> str:
  return _restyle(str(ULID()))


def _restyle(ulid_string: str) -> str:
  lowered = ulid_string.lower()
  return f'{lowered[:10]}-{lowered[10:18]}-{lowered[18:]}'
