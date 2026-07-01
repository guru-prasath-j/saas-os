"""Prebuild / refresh the vector index over the vault."""
from amy import vault as v
from amy.index import build_index
notes = v.load_notes()
idx, backend = build_index(notes)
print(f"indexed {len(notes)} notes using '{backend}' backend")
