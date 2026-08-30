import importlib.metadata

from bro.local.prompts import FRAMEWORK_PROJECT
from bros.bro_dev import AUTHORING
from bros.bro_eyebro import BroEyebro


def test_bro_eyebro_is_registered_as_a_persona():
  entries = importlib.metadata.entry_points(group='bro', name=BroEyebro.name)
  assert [entry.load() for entry in entries] == [BroEyebro]


def test_bro_eyebro_carries_the_framework_context_without_the_author_block():
  prompt = BroEyebro().system_prompt
  assert FRAMEWORK_PROJECT.strip() in prompt
  assert AUTHORING.strip() not in prompt
