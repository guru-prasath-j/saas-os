"""Memory layer — the Operational layer that makes the Obsidian vault the
'memory lake' (Phase 1 writer + Phase 2 journaling bridge + Phase 3 auto-linking)."""
from .writer import MemoryWriter, DAILY_DIR, MEMORY_DIR
from .journal import attach_journal, JournalSync
from .entities import EntityIndex
from .recall import MemoryRecall
from .consolidate import Consolidator, WEEKLY_DIR
from .reindex import VaultReindex
