from __future__ import annotations

import base64
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  from openai.types.responses.response_input_file_param import ResponseInputFileParam
  from openai.types.responses.response_input_image_param import ResponseInputImageParam
  from openai.types.responses.response_input_text_param import ResponseInputTextParam


def image_to_content(data: bytes, mime_type: str) -> ResponseInputImageParam:
  from openai.types.responses.response_input_image_param import ResponseInputImageParam

  encoded = base64.b64encode(data).decode('utf-8')
  image_url = f'data:{mime_type};base64,{encoded}'
  return ResponseInputImageParam(type='input_image', image_url=image_url, detail='high')


def png_to_content(data: bytes) -> ResponseInputImageParam:
  return image_to_content(data, 'image/png')


def image_file_to_content(image_path: str) -> ResponseInputImageParam:
  if not image_path.endswith('.png'):
    raise NotImplementedError('only PNG images supported')
  with open(image_path, 'rb') as f:
    return png_to_content(f.read())


def pdf_to_content(data: bytes, filename: str) -> ResponseInputFileParam:
  from openai.types.responses.response_input_file_param import ResponseInputFileParam

  encoded = base64.b64encode(data).decode('utf-8')
  # OpenAI's input_file rejects filenames containing path separators (e.g.
  # "Payslip4/2026.pdf" comes back as 400 "badly formatted or corrupted").
  safe_filename = filename.replace('/', '_').replace('\\', '_') or 'file.pdf'
  return ResponseInputFileParam(
    type='input_file', file_data=f'data:application/pdf;base64,{encoded}', filename=safe_filename
  )


def text_to_content(text: str) -> ResponseInputTextParam:
  from openai.types.responses.response_input_text_param import ResponseInputTextParam

  return ResponseInputTextParam(type='input_text', text=text)
