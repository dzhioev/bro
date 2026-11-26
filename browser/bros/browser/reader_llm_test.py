from pathlib import Path

from bro import tool_files
from bro.base.suite_environment import host_credential_store
from bros.browser import mcp

SNAPSHOT_REF = f'sha256:{"4" * 64}/snapshot.yml'
SNAPSHOT = Path(__file__).parents[2] / 'testdata' / 'browser-reader-snapshot.yml'


def test_reader_answers_over_a_real_snapshot_with_only_matching_elements(monkeypatch) -> None:
  def fetch(ref: str, *, checked: bool) -> str:
    assert ref == SNAPSHOT_REF
    assert checked
    return str(SNAPSHOT)

  monkeypatch.setattr(tool_files, 'fetch_artifact', fetch)

  with host_credential_store():
    result = mcp.look(
      'What version and date are shown, and which control opens its release notes?',
      ref=SNAPSHOT_REF,
    )

  assert '5.6' in result['answer']
  assert 'October 8' in result['answer']
  assert result['elements']
  content = SNAPSHOT.read_text()
  assert all(
    mcp._element_present(content, mcp._ReaderElement.model_validate(element))
    for element in result['elements']
  )
