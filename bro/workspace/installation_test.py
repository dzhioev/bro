import pytest

from bro.workspace.installation import MARKER, installation_home


def test_marker_selects_an_undeclared_home(installation_environment):
  (installation_environment / MARKER).write_text('another-home\n')
  assert installation_home() == 'another-home'


@pytest.mark.parametrize(
  'content', [None, '', 'bad name\n', 'Wrong\n', 'home', 'home\n\n', '../home\n']
)
def test_bad_marker_names_the_path_and_how_to_write_it(installation_environment, content):
  marker = installation_environment / MARKER
  if content is None:
    marker.unlink()
  else:
    marker.write_text(content)
  with pytest.raises(ValueError) as failure:
    installation_home()
  assert str(marker) in str(failure.value)
  assert "printf '<home>\\n' >" in str(failure.value)
