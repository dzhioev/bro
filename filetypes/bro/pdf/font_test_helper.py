"""Fresh-process font-path and confinement probe."""

import ctypes
import json
import sys
from pathlib import Path

import pypdfium2_raw as raw

from bro.files.confinement import SandboxLimits, apply
from bro.pdf import TYPE
from bro.pdf.page_text import assemble
from bro.pdf.pdf_test_helper import make_pdf
from bro.pdf.provider import provider


def probe() -> None:
  directory, confined = sys.argv[1:]
  path = ctypes.create_string_buffer(str(Path(directory)).encode())
  paths = (ctypes.POINTER(ctypes.c_char) * 2)(path, None)
  config = raw.FPDF_LIBRARY_CONFIG(version=2, m_pUserFontPaths=paths)
  raw.FPDF_InitLibraryWithConfig(ctypes.byref(config))
  provider.prepare()
  data = make_pdf(['HelloWorld', 'Invoice INV-2024-001'])
  if confined == 'yes':
    apply(SandboxLimits())
  texts = []
  with provider.engine.document(data, TYPE.limits) as document:
    assert document is not None
    for index in range(document.page_count):
      with document.page(index) as page:
        assert page is not None
        texts.append(assemble(page.characters()))
  print(json.dumps(texts))


if __name__ == '__main__':
  probe()
