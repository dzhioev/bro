import subprocess
from pathlib import Path
from types import SimpleNamespace

import cw.git


def _git(*args: str, cwd: Path) -> None:
  subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
  path.mkdir(parents=True, exist_ok=True)
  _git('init', '-q', '-b', 'master', cwd=path)


def _commit(path: Path, name: str) -> str:
  (path / name).write_text(name)
  _git('add', name, cwd=path)
  _git('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-q', '-m', name, cwd=path)
  return cw.git.git_out('rev-parse', 'HEAD', cwd=str(path))


def _private_refs(path: Path) -> str:
  return cw.git.git_out('for-each-ref', 'refs/cw', cwd=str(path))


def _forbid_transfer(monkeypatch):
  real = cw.git.git_run

  def guard(*args, cwd=None, env=None):
    assert args[0] not in ('fetch', 'push'), 'must resolve without a transfer'
    return real(*args, cwd=cwd, env=env)

  monkeypatch.setattr(cw.git, 'git_run', guard)


class TestRevParseCommit:
  def test_resolves_to_sha(self, monkeypatch):
    captured: dict = {}

    def fake_git_run(*args, cwd=None, env=None):
      captured['args'] = args
      captured['cwd'] = cwd
      return SimpleNamespace(returncode=0, stdout='abc123\n')

    monkeypatch.setattr(cw.git, 'git_run', fake_git_run)
    assert cw.git.rev_parse_commit(Path('/repo'), 'master') == 'abc123'
    assert captured['args'] == ('rev-parse', '--verify', 'master^{commit}')
    assert captured['cwd'] == Path('/repo')

  def test_none_when_unresolvable(self, monkeypatch):
    monkeypatch.setattr(
      cw.git, 'git_run', lambda *a, **k: SimpleNamespace(returncode=128, stdout='')
    )
    assert cw.git.rev_parse_commit(Path('/repo'), 'nope') is None


class TestResolveRef:
  def test_resolves_local_ref_without_fetching(self, tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    _init_repo(repo)
    sha = _commit(repo, 'a')
    _forbid_transfer(monkeypatch)
    assert cw.git.resolve_ref(repo, 'master') == sha

  def test_fetches_origin_when_ref_not_local(self, tmp_path):
    origin = tmp_path / 'origin'
    _init_repo(origin)
    _commit(origin, 'a')
    _git('checkout', '-q', '-b', 'feature', cwd=origin)
    feature_sha = _commit(origin, 'b')
    local = tmp_path / 'local'
    _init_repo(local)
    _commit(local, 'base')
    _git('remote', 'add', 'origin', str(origin), cwd=local)
    assert cw.git.resolve_ref(local, 'feature') == feature_sha
    # the fetched objects stay usable, and the private resolve ref is gone
    assert cw.git.rev_parse_commit(local, feature_sha) == feature_sha
    assert _private_refs(local) == ''

  def test_none_when_neither_resolves(self, tmp_path):
    local = tmp_path / 'local'
    _init_repo(local)
    _commit(local, 'a')
    _git('remote', 'add', 'origin', str(tmp_path / 'no-such-remote'), cwd=local)
    assert cw.git.resolve_ref(local, 'nope') is None
    assert _private_refs(local) == ''

  def test_fetch_targets_a_unique_private_ref_with_the_no_prompt_overlay(self, monkeypatch):
    # mocked: the env overlay and the per-call refspec are invisible to real git
    fetches: list = []

    def fake_git_run(*args, cwd=None, env=None):
      if args[0] == 'fetch':
        fetches.append({'args': args, 'env': env})
        return SimpleNamespace(returncode=1, stdout='')
      return SimpleNamespace(returncode=128, stdout='')

    monkeypatch.setattr(cw.git, 'git_run', fake_git_run)
    assert cw.git.resolve_ref(Path('/repo'), 'nope') is None
    assert cw.git.resolve_ref(Path('/repo'), 'nope') is None
    refspecs = []
    for fetch in fetches:
      assert fetch['args'][1] == 'origin'
      refspec = fetch['args'][2]
      assert refspec.startswith('+nope:refs/cw/resolve-')
      refspecs.append(refspec)
      # no-prompt overlay: an unreachable remote fails fast instead of prompting
      assert fetch['env']['GIT_TERMINAL_PROMPT'] == '0'
    # nonce-named: concurrent resolutions cannot read each other's result
    assert len(set(refspecs)) == 2


class TestResolveHead:
  def _root_and_clone(self, tmp_path) -> tuple[Path, Path]:
    root = tmp_path / 'root'
    _init_repo(root)
    _commit(root, 'a')
    workspace = tmp_path / 'workspace'
    _git('clone', '-q', '--shared', str(root), str(workspace), cwd=tmp_path)
    return root, workspace

  def test_shared_store_resolves_without_a_transfer(self, tmp_path, monkeypatch):
    root, workspace = self._root_and_clone(tmp_path)
    _forbid_transfer(monkeypatch)
    assert cw.git.resolve_head(root, workspace) == cw.git.rev_parse_commit(root, 'HEAD')

  def test_local_commits_transfer_into_root(self, tmp_path):
    root, workspace = self._root_and_clone(tmp_path)
    local_sha = _commit(workspace, 'local')
    assert cw.git.rev_parse_commit(root, local_sha) is None
    assert cw.git.resolve_head(root, workspace) == local_sha
    # the commit's objects are now in root's store (a child clone reads them via
    # /host-repo alternates), with no private ref left behind
    assert cw.git.rev_parse_commit(root, local_sha) == local_sha
    assert _private_refs(root) == ''

  def test_resolves_a_clone_whose_alternates_point_elsewhere(self, tmp_path):
    # a container clone's alternates file names /host-repo — a path valid only in
    # its own mount namespace. the alternates env overlay must carry both the
    # HEAD read and the object transfer regardless.
    root, workspace = self._root_and_clone(tmp_path)
    local_sha = _commit(workspace, 'local')
    alternates = workspace / '.git' / 'objects' / 'info' / 'alternates'
    assert alternates.is_file()
    alternates.write_text('/host-repo/.git/objects\n')
    assert cw.git.resolve_head(root, workspace) == local_sha
    assert cw.git.rev_parse_commit(root, local_sha) == local_sha
    assert _private_refs(root) == ''

  def test_none_when_the_repository_is_missing(self, tmp_path):
    root = tmp_path / 'root'
    _init_repo(root)
    _commit(root, 'a')
    assert cw.git.resolve_head(root, tmp_path / 'gone') is None
