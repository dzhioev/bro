# built-in bros are defined here as name -> "module:ClassName"; installed
# distributions contribute others through the `bro` entry-point group. the registry
# imports a bro's module lazily on first lookup by name, so resolving one bro never
# imports another's dependency graph. the map carries no imports itself, so reading
# it is free; the key must equal the class's own
# `name` attribute (the registry validates this on load).
BRO_SPECS: dict[str, str] = {
  'analyst': 'bros.analyst:Analyst',
  'bro': 'bros.bro:Bro',
  'dev': 'bros.dev:Dev',
  'lead': 'bros.lead:Lead',
}
