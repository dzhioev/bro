"""the harness × LLM recipe matrix the conformance probes run on, and the launch
they share.

A conformance probe holds one behavior of the framework's contract on every
installed harness, under each recipe this checkout supports there. A probe
launches as an operator does, `ride solo` unboxed and unattended, so its pass
covers the launch, the harness, and the model together; the session records its
trail locally, and the probe reads its verdict off that trail."""

import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from bro.base import configs
from bro.base.spawn import console_script
from bro.harness import installed_harness_names
from bro.trails.local import LocalStore
from bro.workspace.paths import ride_trails_dir

# the recipes each harness is supported under, as `--llm` takes them
RECIPES: dict[str, tuple[str, ...]] = {
  'bro': ('openai:terra:medium', 'openai:sol:high'),
  'claude': ('claude-code:opus:xhigh',),
}

_SESSION_TIMEOUT_SECONDS = 900


def harness_recipes() -> list:
  """one `(harness, recipe)` parameter per installed harness and supported
  recipe; an installed harness with no supported recipe fails collection."""
  installed = installed_harness_names()
  unlisted = sorted(set(installed) - RECIPES.keys())
  if len(unlisted) > 0:
    raise LookupError(
      f'conformance lists no recipe for installed harness(es): {", ".join(unlisted)}'
    )
  return [
    pytest.param(harness, recipe, id=f'{harness}-{recipe}')
    for harness in installed
    for recipe in RECIPES[harness]
  ]


def data_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
  """the runtime data root every probe of a pytest run launches under, so the
  runtime bundle ride freezes and the harness binaries it fetches are paid once
  per run rather than once per probe."""
  path = tmp_path_factory.getbasetemp() / 'conformance-data'
  path.mkdir(exist_ok=True)
  return path


def run_unattended(
  *, harness: str, recipe: str, bro: str, prompt: str, tree: Path, data: Path
) -> list[dict]:
  """run `prompt` as an unattended unboxed session of `bro` on `harness` under
  `recipe`, in the existing directory `tree` with runtime data root `data`, and
  return the messages of the trail it recorded."""
  environment = {**os.environ, 'XDG_DATA_HOME': str(data), 'BRO_STORE': str(configs.STORE_DIR)}
  with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(data)}):
    trails = LocalStore(ride_trails_dir())
  before = set(trails.stored_trail_ids())
  launch = [
    console_script('ride'),
    'solo',
    '--unboxed',
    '--tree',
    str(tree),
    '--harness',
    harness,
    '--llm',
    recipe,
    '--hold',
    'unattended',
    '--revoke',
    'trails',
    bro,
    prompt,
  ]
  completed = subprocess.run(
    launch,
    env=environment,
    capture_output=True,
    text=True,
    timeout=_SESSION_TIMEOUT_SECONDS,
  )
  assert completed.returncode == 0, completed.stderr
  recorded = sorted(set(trails.stored_trail_ids()) - before)
  assert len(recorded) == 1, (recorded, completed.stderr)
  return list(trails.iter_messages(recorded[0]))
