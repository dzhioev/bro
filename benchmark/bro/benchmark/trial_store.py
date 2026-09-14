"""the local trail store a trial's run leaves under its collected agent directory."""

from pathlib import Path
from typing import Optional

from bro.llm import usage
from bro.llm.usage import Counts
from bro.trails.local import LocalStore

# the store's data home is the agent directory, so its layout sits at this path
# below it
TRAILS_DIRECTORY = Path('ride') / 'trails'


def recorded_a_trail(store_root: Path) -> bool:
  """whether the store holds one. Opening a store creates its own directories,
  so a run that ended before blazing a trail leaves the layout behind with
  nothing in it."""
  if not (store_root / 'trails').is_dir():
    return False
  with LocalStore(store_root) as store:
    return next(store.iter_trails(max_items=1), None) is not None


def token_totals(store_root: Path) -> Optional[Counts]:
  """the token classes summed over every LLM call of every trail the store
  holds, or None when it holds no trail."""
  if not recorded_a_trail(store_root):
    return None
  totals = usage.zero()
  with LocalStore(store_root) as store:
    for header in store.iter_trails():
      for message in store.iter_messages(header['id'], types={'llm_call'}):
        raw_usage = message.get('usage')
        if not isinstance(raw_usage, dict):
          raise ValueError('llm_call usage must be an object')
        totals = usage.add(totals, usage.from_vendor_counts(raw_usage))
  return totals
