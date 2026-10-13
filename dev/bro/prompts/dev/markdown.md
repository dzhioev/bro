# Markdown

- Write markdown prose one sentence per source line, splitting a long sentence further at its major clause boundaries
  — a semicolon, a colon that introduces a clause, the em-dash clause whose dash then opens the new line.
  Never strand a bare label or a lone reference on a line of its own:
  what a break leaves on either side has to read as a clause, so the em dash separating a list item's term from its gloss is punctuation rather than a boundary, and the term keeps the start of its gloss.
  A reword then shows up in review as the clause it touched.
  Line length is not the unit: a short sentence is a short line, and nothing is padded out to a column.
  Never break inside a code block or a table row, or anywhere adjacent to a `{{…}}` group, whose surrounding whitespace renders literally;
  keep a continuation line in its list item's content column, and repeat a `>` marker on every line of a quoted paragraph.
  `check-markdown` holds a reflow to whitespace and flags a paragraph left as one source line.
  Text posted where a single newline renders as a line break
  — GitHub issue, pull request, and comment bodies, through `brog` or `gh`
  — keeps each paragraph on one line instead.
