import contextlib
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import ride.attachments as attachments
import ride.do_ride as do_ride
import ride.session as session
import ride.workspace.docker as workspace_docker
from ride.harness import ContainerExtras, SessionHarness
from ride.session_test import _launch_scope, _runtime_bundle, _spec
from ride.workspace.docker import ContainerRuntime, ContainerRuntimeResolver
from ride.workspace.metadata import Isolation
from ride.workspace.model import Workspace


@pytest.fixture(autouse=True)
def isolated_environment():
  with patch.dict(os.environ):
    yield


_PROVIDER = """
import contextlib
import os
from ride.attachments import Attachment, AttachmentRequest, AttachmentType, ResolvedAttachment

class Notes(AttachmentType):
  name = 'notes'

  def validate(self, attachment):
    if attachment.fields.keys() != {'content'}:
      raise ValueError('notes require content')

  def resolve(self, request, workspace):
    return contextlib.nullcontext(ResolvedAttachment(Attachment(self.name, 'notes', request.fields)))

  def identity(self, attachment):
    return attachment.fields

  def prepare(self, attachment, directory, workspace, base):
    directory.mkdir()
    (directory / 'content').write_text(attachment.fields['content'])

  def environment(self, attachment, directory):
    return {'ATTACHMENT_CONTENT': (directory / 'content').read_text()}

  def start_session(self, attachment, directory, bro):
    assert os.environ['CREDENTIALS_READY'] == '1'
    (directory / 'started').write_text(bro)

  def inherit(self, attachment, directory, into):
    return AttachmentRequest(self.name, attachment.fields)

  def cleanliness(self, attachment, directory, session_end):
    return session_end

  def remove(self, attachment, directory, *, force):
    pass

  def summary(self, attachment):
    return attachment.name

  def clean_shared(self, attachments, *, dry_run):
    pass

notes = Notes()
"""


@contextlib.contextmanager
def _installed_notes(tmp_path, monkeypatch):
  distribution = tmp_path / 'attachment_probe-1.0.dist-info'
  distribution.mkdir()
  (distribution / 'METADATA').write_text('Name: attachment-probe\nVersion: 1.0\n')
  (distribution / 'entry_points.txt').write_text(
    '[bro.attachment_types]\nnotes = attachment_probe:notes\n'
  )
  (tmp_path / 'attachment_probe.py').write_text(_PROVIDER)
  with monkeypatch.context() as patch:
    patch.syspath_prepend(str(tmp_path))
    attachments._entry_points.cache_clear()
    try:
      yield
    finally:
      attachments._entry_points.cache_clear()


@pytest.mark.parametrize('isolation', [Isolation.BOXED, Isolation.UNBOXED])
def test_installed_type_prepares_contributes_facts_and_starts_through_the_contract(
  tmp_path,
  monkeypatch,
  isolation,
):
  with _installed_notes(tmp_path, monkeypatch):
    assert 'notes' in attachments.installed_attachment_types()
    request = attachments.AttachmentRequest('notes', {'content': 'provided data'})
    with attachments.resolve_attachment(request, 'notes-session') as resolved:
      assert resolved is not None
      workspace = Workspace.create('notes-session', (resolved.attachment,), isolation)
    harness = MagicMock(spec=SessionHarness)
    harness.container_extras.return_value = ContainerExtras({}, ())
    harness.run_session.return_value = 0
    monkeypatch.setattr(session, 'get_harness', lambda name: harness)
    monkeypatch.setattr(session, 'find_container_id', lambda tree: None)
    spec = _spec(
      name=workspace.name, isolation=isolation, solo=True, prompt='read notes', no_trails=True
    )
    launch = session.started_party_launch(
      spec,
      workspace,
      None,
      _launch_scope(),
      runtime_bundle=_runtime_bundle(tmp_path),
      container_runtime=ContainerRuntimeResolver.fixed(ContainerRuntime('image', 'bundle')),
      env={},
      credential_directory=tmp_path / 'credentials',
      install_directory=tmp_path / 'environment',
    )
    if isolation is Isolation.BOXED:
      assert isinstance(launch, workspace_docker.Launch)
      assert not workspace.start_directory.exists()
      create = MagicMock(return_value='container')
      monkeypatch.setattr(workspace_docker, '_create_container', create)
      assert workspace_docker.prepare_container(launch) == 'container'
      argv = create.call_args.args[0]
      assert argv[argv.index('-w') + 1] == '/tree/notes'
      environment = dict(
        argv[index + 1].split('=', 1) for index, value in enumerate(argv[:-1]) if value == '-e'
      )
    else:
      assert isinstance(launch, session.ProcessLaunch)
      assert Path(launch.cwd) == workspace.tree / 'notes'
      environment = launch.env
    assert environment['ATTACHMENT_CONTENT'] == 'provided data'
    encoded = launch.command[launch.command.index('--attachment') + 1]
    assert json.loads(encoded) == workspace.attachments[0].dump()
    inherited = attachments.inherited_attachment(workspace.attachments, workspace.tree, None)
    assert inherited == request
    for key, value in environment.items():
      monkeypatch.setenv(key, value)
    monkeypatch.setenv('RIDE_SESSION_DIR', str(workspace.path / 'session'))
    monkeypatch.chdir(workspace.start_directory)
    monkeypatch.setattr(
      do_ride, '_install_credential_hooks', lambda: monkeypatch.setenv('CREDENTIALS_READY', '1')
    )
    monkeypatch.setattr(do_ride, 'session_broxy', contextlib.nullcontext)
    monkeypatch.setattr(do_ride.watches, 'session_watch_admitted', lambda: False)
    monkeypatch.setattr(do_ride.watches.Owner, 'for_session', contextlib.nullcontext)
    run = do_ride.SessionRun(
      name=workspace.name,
      attachments=workspace.attachments,
      harness='bro',
      hold=spec.hold,
      resolved_recipe=spec.resolved_recipe,
      solo=True,
      resume=False,
      bro=spec.bro,
      prompt='read notes',
      activity_file=workspace.path / 'session' / 'activity',
    )
    assert do_ride.run_session(harness, run) == 0
    assert (workspace.start_directory / 'started').read_text() == spec.bro
    (workspace.tree / 'beside-attachment').write_text('session data')
    assert Path.cwd() == workspace.start_directory


@pytest.mark.parametrize('name', ['', '.', '..', '../escape', '/absolute', 'two/names'])
def test_attachment_names_stay_one_tree_component(name):
  with pytest.raises(ValueError, match='attachment name'):
    attachments.Attachment('repo', name, {'repo': '/repo', 'branch': 'branch'})


def _git(directory, *arguments):
  import subprocess

  return subprocess.check_output(['git', '-C', str(directory), *arguments], text=True).strip()


def _source_repository(tmp_path):
  source = tmp_path / 'project'
  source.mkdir()
  _git(source, 'init', '--quiet')
  _git(source, 'config', 'user.name', 'Example Human')
  _git(source, 'config', 'user.email', 'human@example.test')
  _git(source, 'remote', 'add', 'origin', 'https://example.test/owner/project.git')
  config = source / '.bro' / 'config.toml'
  config.parent.mkdir()
  config.write_text(
    'provision-command = \'test "$CREDENTIALS_READY" = 1 && pwd > provision-directory && touch ../session-note\'\n'
  )
  _git(source, 'add', '.')
  _git(source, 'commit', '--quiet', '-m', 'initial')
  return source


def test_unboxed_repo_launch_starts_in_clone_and_provisions_with_room_beside_it(
  tmp_path, monkeypatch
):
  import ride.repo_attachment as repository

  source = _source_repository(tmp_path)
  with attachments.resolve_attachment(
    attachments.AttachmentRequest('repo', {'repo': str(source)}), 'repo-session'
  ) as resolved:
    assert resolved is not None
    workspace = Workspace.create('repo-session', (resolved.attachment,), Isolation.UNBOXED)
  harness = MagicMock(spec=SessionHarness)
  harness.run_session.return_value = 0
  monkeypatch.setattr(session, 'get_harness', lambda name: harness)
  spec = _spec(name=workspace.name, isolation=Isolation.UNBOXED)
  launch = session.started_party_launch(
    spec,
    workspace,
    resolved.base,
    _launch_scope(),
    runtime_bundle=_runtime_bundle(tmp_path),
    container_runtime=ContainerRuntimeResolver(None),
    env={},
    credential_directory=tmp_path / 'credentials',
    install_directory=tmp_path / 'environment',
  )
  assert isinstance(launch, session.ProcessLaunch)
  assert Path(launch.cwd) == workspace.tree / source.name
  assert launch.env['RIDE_BASE_SHA'] == _git(source, 'rev-parse', 'HEAD')
  assert launch.env['RIDE_REPO_URL'] == 'https://example.test/owner/project.git'
  for key, value in launch.env.items():
    monkeypatch.setenv(key, value)
  monkeypatch.chdir(launch.cwd)
  monkeypatch.setattr(
    do_ride, '_install_credential_hooks', lambda: monkeypatch.setenv('CREDENTIALS_READY', '1')
  )
  monkeypatch.setattr(do_ride, 'session_broxy', contextlib.nullcontext)
  monkeypatch.setattr(do_ride.watches, 'session_watch_admitted', lambda: False)
  monkeypatch.setattr(do_ride.watches.Owner, 'for_session', contextlib.nullcontext)
  persona = MagicMock()
  monkeypatch.setattr(repository, 'create_bro', lambda name: persona)
  run = do_ride.SessionRun(
    name=workspace.name,
    attachments=workspace.attachments,
    harness='bro',
    hold=spec.hold,
    resolved_recipe=spec.resolved_recipe,
    solo=True,
    resume=False,
    bro=spec.bro,
    prompt='read project',
    activity_file=workspace.path / 'session' / 'activity',
  )
  assert do_ride.run_session(harness, run) == 0
  assert (workspace.start_directory / 'provision-directory').read_text().strip() == str(
    workspace.start_directory
  )
  assert (workspace.tree / 'session-note').is_file()
  persona.provision_workspace.assert_called_once_with(workspace.start_directory)


def test_inherited_repo_bases_are_read_when_the_child_is_launched(tmp_path):

  source = _source_repository(tmp_path)
  with attachments.resolve_attachment(
    attachments.AttachmentRequest('repo', {'repo': str(source)}), 'parent'
  ) as parent:
    assert parent is not None
    workspace = Workspace.create('parent', (parent.attachment,), Isolation.UNBOXED)
  workspace.prepare(parent.base)
  inherited = attachments.inherited_attachment(workspace.attachments, workspace.tree, None)
  first = _git(workspace.start_directory, 'rev-parse', 'HEAD')
  with attachments.resolve_attachment(inherited, 'started') as started:
    assert started is not None
    assert started.base == first
    _git(
      workspace.start_directory,
      '-c',
      'user.name=Agent',
      '-c',
      'user.email=agent@example.test',
      'commit',
      '--quiet',
      '--allow-empty',
      '-m',
      'after summon',
    )
    second = _git(workspace.start_directory, 'rev-parse', 'HEAD')
    child = Workspace.create('started', (started.attachment,), Isolation.UNBOXED)
    child.prepare(started.base)
    assert _git(child.start_directory, 'rev-parse', 'HEAD') == first
  with attachments.resolve_attachment(inherited, 'manual') as manual:
    assert manual is not None
    child = Workspace.create('manual', (manual.attachment,), Isolation.UNBOXED)
    child.prepare(manual.base)
    assert _git(child.start_directory, 'rev-parse', 'HEAD') == second
