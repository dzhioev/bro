import subprocess
from pathlib import Path

PRELUDE = Path(__file__).parent / 'prelude.sh'


def test_here_is_the_executed_script_directory_from_any_sourcing_depth(tmp_path):
  real = tmp_path / 'real'
  (real / 'tool').mkdir(parents=True)
  (real / 'lib').mkdir()
  script = real / 'tool' / 'main.sh'
  script.write_text(
    f'#!/usr/bin/env -S bash -e\nsource "{PRELUDE}"\nsource "$HERE/../lib/outer.sh"\necho "$HERE"\n'
  )
  script.chmod(0o755)
  (real / 'lib' / 'outer.sh').write_text('source "$(dirname "${BASH_SOURCE[0]}")/inner.sh"\n')
  # re-sourced two levels down, where the immediate sourcer is not the executed script
  (real / 'lib' / 'inner.sh').write_text(f'source "{PRELUDE}"\necho "$HERE"\n')
  link = tmp_path / 'link'
  link.symlink_to('real')

  result = subprocess.run(
    [str(link / 'tool' / 'main.sh')], capture_output=True, text=True, check=False
  )

  assert result.returncode == 0, result.stderr
  assert result.stdout.splitlines() == [str((real / 'tool').resolve())] * 2
