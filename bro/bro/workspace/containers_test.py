import json

import pytest

import cw.containers


class _FakeProc:
  def __init__(self, returncode=0, stdout='', stderr: str | bytes = ''):
    self.returncode = returncode
    self.stdout = stdout
    self.stderr = stderr


class TestSeedContainerClaudeJSON:
  def _host(self, tmp_path, **extra):
    path = tmp_path / 'host.json'
    path.write_text(json.dumps({'oauthAccount': {'id': 'acct'}, 'userID': 'uid', **extra}))
    return path

  def _seed_dir(self, tmp_path):
    d = tmp_path / 'seed'
    d.mkdir()
    return d

  def test_constructs_explicit_config_plus_identity(self, tmp_path):
    host = self._host(tmp_path, projects={'/x': {}}, numStartups=42)
    seed = cw.containers._seed_container_claude_json(self._seed_dir(tmp_path), host)
    data = json.loads(seed.read_text())
    assert data['installMethod'] == 'global'
    assert data['hasCompletedOnboarding'] is True
    assert data['projects']['/workspace']['hasTrustDialogAccepted'] is True
    assert data['oauthAccount'] == {'id': 'acct'}
    assert data['userID'] == 'uid'
    # host machine state must not leak in
    assert '/x' not in data['projects']
    assert 'numStartups' not in data

  def test_missing_host_file_is_fatal(self, tmp_path):
    with pytest.raises(SystemExit):
      cw.containers._seed_container_claude_json(self._seed_dir(tmp_path), tmp_path / 'absent.json')

  def test_missing_identity_key_is_fatal(self, tmp_path):
    host = tmp_path / 'host.json'
    host.write_text(json.dumps({'userID': 'uid'}))
    with pytest.raises(SystemExit):
      cw.containers._seed_container_claude_json(self._seed_dir(tmp_path), host)

  def test_seed_is_not_overwritten_on_second_call(self, tmp_path):
    seed_dir = self._seed_dir(tmp_path)
    seed = cw.containers._seed_container_claude_json(seed_dir, self._host(tmp_path))
    seed.write_text(json.dumps({'container': 'wrote-this'}))
    again = cw.containers._seed_container_claude_json(seed_dir, self._host(tmp_path))
    assert json.loads(again.read_text()) == {'container': 'wrote-this'}


class TestReplaceContainerResumeHint:
  def test_replace_container_resume_hint_prints_recorded_command(
    self, monkeypatch, capsys, tmp_path
  ):
    monkeypatch.setenv('CW_RESUME_COMMAND', 'cw ss -c --auto --resume w')
    monkeypatch.setattr(cw.containers, '_project_root', lambda: tmp_path)
    monkeypatch.setattr(cw.containers, '_latest_jsonl', lambda d: 'session.jsonl')
    monkeypatch.setattr('sys.stdout.isatty', lambda: True)
    cw.containers._replace_container_resume_hint('w')
    assert 'cw ss -c --auto --resume w' in capsys.readouterr().out


class TestRunInContainerInjection:
  @pytest.fixture
  def harness(self, monkeypatch, tmp_path):
    monkeypatch.setattr(cw.containers, '_project_root', lambda: tmp_path / 'proj')
    monkeypatch.setattr(cw.containers, '_image_tag', lambda: 'tag')
    monkeypatch.setattr(cw.containers, '_ensure_image', lambda tag: None)
    monkeypatch.setattr(cw.containers.Path, 'home', lambda: tmp_path / 'home')
    monkeypatch.setattr(
      cw.containers, '_docker_create_argv', lambda *a, **k: ['docker', 'create', 'ARGS']
    )
    monkeypatch.setattr(
      cw.containers.credentials,
      'build_scoped_store',
      lambda names, optional=(): {'credentials.json': b'{}'},
    )
    calls: list = []

    def fake_run(argv, *a, **k):
      calls.append({'argv': argv, 'input': k.get('input')})
      if argv[0] == 'git':
        return _FakeProc(returncode=0)
      if argv[:2] == ['docker', 'create']:
        return _FakeProc(returncode=0, stdout='cid123\n')
      if argv[:2] == ['docker', 'start']:
        return _FakeProc(returncode=7)
      return _FakeProc(returncode=0)  # cp, rm

    monkeypatch.setattr(cw.containers.subprocess, 'run', fake_run)
    return calls

  def test_create_cp_start_sequence(self, harness):
    import io
    import tarfile

    code = cw.containers.run_in_container('ws', ['claude'])
    assert code == 7  # propagates `docker start` exit code
    docker_calls = [c for c in harness if c['argv'][0] == 'docker']
    assert docker_calls[0]['argv'][:2] == ['docker', 'create']
    # the scoped store is cp'd into the pre-start container as a tar on stdin
    cp = next(c for c in harness if c['argv'][:3] == ['docker', 'cp', '-'])
    assert cp['argv'][3] == 'cid123:/home/cw'
    assert isinstance(cp['input'], bytes)
    with tarfile.open(fileobj=io.BytesIO(cp['input']), mode='r') as tar:
      assert '.ppp/credentials.json' in tar.getnames()
    # last call is the run-equivalent `docker start -a -i <id>`
    assert harness[-1]['argv'] == ['docker', 'start', '-a', '-i', 'cid123']

  def test_bare_session_synced_host_side(self, harness, monkeypatch):
    # `--bare` (the --bro flow) runs hooks-free, so cw uploads the transcript
    # itself after the container exits
    synced: list = []
    monkeypatch.setattr(
      cw.containers, '_sync_container_log', lambda name, proj: synced.append(name)
    )
    cw.containers.run_in_container('ws', ['claude', '--bare'])
    assert synced == ['ws']

  def test_native_session_not_synced_host_side(self, harness, monkeypatch):
    # native sessions self-upload via their in-container hooks; no host-side sync
    synced: list = []
    monkeypatch.setattr(
      cw.containers, '_sync_container_log', lambda name, proj: synced.append(name)
    )
    cw.containers.run_in_container('ws', ['claude'])
    assert synced == []

  def test_cp_failure_removes_container_and_raises(self, monkeypatch, tmp_path):
    monkeypatch.setattr(cw.containers, '_project_root', lambda: tmp_path / 'proj')
    monkeypatch.setattr(cw.containers, '_image_tag', lambda: 'tag')
    monkeypatch.setattr(cw.containers, '_ensure_image', lambda tag: None)
    monkeypatch.setattr(cw.containers.Path, 'home', lambda: tmp_path / 'home')
    monkeypatch.setattr(cw.containers, '_docker_create_argv', lambda *a, **k: ['docker', 'create'])
    monkeypatch.setattr(
      cw.containers.credentials,
      'build_scoped_store',
      lambda names, optional=(): {'credentials.json': b'{}'},
    )
    removed: list = []

    def fake_run(argv, *a, **k):
      if argv[0] == 'git':
        return _FakeProc(returncode=0)
      if argv[:2] == ['docker', 'create']:
        return _FakeProc(returncode=0, stdout='cid123\n')
      if argv[:3] == ['docker', 'cp', '-']:
        return _FakeProc(returncode=1, stderr=b'no such container')
      if argv[:3] == ['docker', 'rm', '-f']:
        removed.append(argv)
        return _FakeProc(returncode=0)
      return _FakeProc(returncode=0)

    monkeypatch.setattr(cw.containers.subprocess, 'run', fake_run)
    with pytest.raises(RuntimeError, match='docker cp'):
      cw.containers.run_in_container('ws', ['claude'])
    assert removed == [['docker', 'rm', '-f', 'cid123']]
