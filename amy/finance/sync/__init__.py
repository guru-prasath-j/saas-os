"""Sync provider abstraction for financial account data ingestion."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SyncResult:
    imported: int = 0
    skipped: int = 0          # duplicates or non-new rows
    errors: list[str] = field(default_factory=list)
    transactions: list[dict] = field(default_factory=list)  # newly imported rows
    preset_detected: str | None = None  # bank_id if auto-detected via bank_presets

    def to_dict(self) -> dict:
        d: dict = {
            "imported": self.imported,
            "skipped": self.skipped,
            "errors": self.errors,
            "transactions": self.transactions,
        }
        if self.preset_detected:
            d["preset_detected"] = self.preset_detected
        return d


class SyncProvider(ABC):
    """Base class for all account sync methods."""

    @property
    @abstractmethod
    def method(self) -> str:
        """Sync method identifier: manual | csv | pdf | gmail | aa"""

    @abstractmethod
    def available(self) -> bool:
        """Returns True if this provider can be used without external credentials."""
