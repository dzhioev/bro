import importlib.metadata
from contextlib import closing

import pytest

from bro.claude import references as claude_references
from bro.local import references
from bro.local.prompts import FRAMEWORK_PROJECT
from bro.monitor import SESSION_DIR_ENV
from bro.native import references as native_references
from bros.bro_dev import AUTHORING, BroDev


def test_bro_dev_is_registered_as_a_persona():
  entries = importlib.metadata.entry_points(group='bro.bros.bro', name=BroDev.name)
  assert [entry.load() for entry in entries] == [BroDev]


def test_checkout_installed_personas_exclude_the_retired_reviewer():
  entries = importlib.metadata.entry_points(group='bro.bros.bro')
  assert 'eyebro' in {entry.name for entry in entries}
  assert 'bro-eyebro' not in {entry.name for entry in entries}


def test_bro_dev_carries_the_framework_context_and_the_author_block():
  prompt = BroDev().composed_prompt('bro', hold='unattended')
  assert FRAMEWORK_PROJECT.strip() in prompt
  assert AUTHORING.strip() in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
  'page',
  [native_references.harness, claude_references.harness, references.bump_claude_code],
  ids=lambda page: page.name,
)
async def test_manual_serves_each_checkout_page_whole(page, monkeypatch, tmp_path):
  monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path))
  with closing(BroDev()) as bro:
    servers = bro.assemble(harness='claude', hold='attended')
    manual = next(server for server in servers if server.namespace == 'man-source')
    [read] = await manual.list_tools()
    assert await read.call({'topic': page.name}) == page._path.read_text()
