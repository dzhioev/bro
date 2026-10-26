from bro.trails.backends import Classification, OpenedBody, ParsedRecord, TrailFormat

TEST_FORMAT = TrailFormat(
  name='test',
  parse=lambda payload: ParsedRecord(None, payload, None, {}, {}),
  classify=lambda record: Classification(),
  project=lambda record: [],
  open=lambda body: OpenedBody(body['records']),
  validate_create=lambda native: None,
  emitted_message_types=frozenset(),
  requires_bro=False,
  owner=lambda header: None,
  native_header_fields=lambda header: [],
)
