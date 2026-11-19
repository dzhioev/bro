import io
import tarfile

from ride.workspace import build_context


def test_runtime_archive_contains_its_inputs_with_normalized_metadata():
  entries, _arguments = build_context.runtime_inputs()
  content = build_context.assemble_runtime(entries)
  assert build_context.assemble_runtime(entries) == content
  with tarfile.open(fileobj=io.BytesIO(content)) as archive:
    files = {member.name: member for member in archive.getmembers() if member.isfile()}
    assert files.keys() == entries.keys()
    for name, (data, mode) in entries.items():
      source = archive.extractfile(name)
      assert source is not None
      assert source.read() == data
      assert files[name].mode == mode
      assert files[name].mtime == 0
