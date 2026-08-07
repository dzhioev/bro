"""root of the source tree the running code was loaded from, derived from this
file's location rather than from any git query."""

from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
