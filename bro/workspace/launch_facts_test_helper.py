"""Launch facts for tests that run a session under some."""

from bro.workspace.launch_facts import LaunchFacts

FACTS = LaunchFacts(
  runtime='b3886e68a777',
  llm='openai:gpt-6.1-sol,effort=high',
  hold='attended',
  summoned=False,
  talk=None,
  may_summon=(),
  permits=('bro for *',),
  may_give=(),
  credentials=('github+', 'mu+', 'trails+'),
)
