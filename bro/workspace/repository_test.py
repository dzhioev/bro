import pytest

from bro.workspace.repository import RepositoryConfig, repository_config


def _write(root, content):
  path = root / '.bro' / 'config.toml'
  path.parent.mkdir(exist_ok=True)
  path.write_text(content)
  return path


def test_absent_configuration_ignores_pyproject(tmp_path):
  (tmp_path / 'pyproject.toml').write_text('[tool.bro]\nprovision-command = "exit 1"\n')
  assert repository_config(tmp_path) == RepositoryConfig()


def test_command_and_owner_tables_are_read_without_interpreting_owner_data(tmp_path):
  _write(
    tmp_path,
    'provision-command = "sh scripts/prepare"\n'
    '[devoops]\ntarget-registry = "example.targets:registry"\n'
    '[other]\nvalues = [1, false]\n[other.nested]\nfuture = "value"\n',
  )
  config = repository_config(tmp_path)
  assert config.provision_command == 'sh scripts/prepare'
  assert config.sections == {
    'devoops': {'target-registry': 'example.targets:registry'},
    'other': {'values': [1, False], 'nested': {'future': 'value'}},
  }


@pytest.mark.parametrize('value', ['""', '3', 'false', '[]', '{}'])
def test_command_requires_a_nonempty_string(tmp_path, value):
  path = _write(tmp_path, f'provision-command = {value}')
  with pytest.raises(ValueError, match='provision-command must be a non-empty string') as error:
    repository_config(tmp_path)
  assert str(path) in str(error.value)


@pytest.mark.parametrize('value', ['"command"', '3', 'false', '[]', '[{}]'])
def test_unknown_top_level_values_refuse_the_file(tmp_path, value):
  path = _write(tmp_path, f'unknown = {value}')
  with pytest.raises(ValueError, match="unknown top-level key 'unknown'") as error:
    repository_config(tmp_path)
  assert str(path) in str(error.value)


def test_malformed_toml_names_the_file(tmp_path):
  path = _write(tmp_path, '[unfinished')
  with pytest.raises(ValueError) as error:
    repository_config(tmp_path)
  assert str(path) in str(error.value)


def test_unreadable_config_is_not_treated_as_absent(tmp_path):
  path = tmp_path / '.bro' / 'config.toml'
  path.mkdir(parents=True)
  with pytest.raises(IsADirectoryError):
    repository_config(tmp_path)
