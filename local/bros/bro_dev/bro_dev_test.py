import importlib.metadata

from bro.local.prompts import FRAMEWORK_PROJECT
from bros.bro_dev import AUTHORING, BroDev


def test_bro_dev_is_registered_as_a_persona():
  entries = importlib.metadata.entry_points(group='bro', name=BroDev.name)
  assert [entry.load() for entry in entries] == [BroDev]


def test_bro_dev_carries_the_framework_context_and_the_author_block():
  prompt = BroDev().system_prompt
  assert FRAMEWORK_PROJECT.strip() in prompt
  assert AUTHORING.strip() in prompt
