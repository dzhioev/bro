# adding a new bro? add it here (name -> "module:ClassName") and nowhere else.
# the registry imports a bro's module lazily, on first lookup by name, so
# `create_bro('pm')` pulls in only PM's dependency graph — not every other bro's
# (librorian's trafilatura/dateparser, devoops's boto3, ...). the map carries no
# imports itself, so reading it is free; the key must equal the class's own
# `name` attribute (the registry validates this on load).
BRO_SPECS: dict[str, str] = {
  'bro': 'bro.bros.bro:Bro',
  'assistant': 'bro.bros.assistant:Assistant',
  'pm': 'bro.bros.pm:PM',
  'librorian': 'bro.bros.librorian:Librorian',
  'devoops': 'bro.bros.devoops:Devoops',
  'dev': 'bro.bros.dev:Dev',
  'ppp-dev': 'bro.bros.ppp_dev:PPPDev',
}
