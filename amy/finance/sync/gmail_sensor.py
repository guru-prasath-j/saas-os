"""GmailSensor — polls Gmail for bank transaction emails via the existing
sync_gmail() function and emits ``finance.gmail_synced`` events.

This wraps the proven gmail_import logic; it does NOT duplicate any parsing.
It is designed to be registered in SensorRegistry and polled on a schedule.

Usage:
    from amy.finance.sync.gmail_sensor import GmailSensor
    sensor = GmailSensor(event_store, fe, creds, llm, account_ids=[...])
    sensor.poll()
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ...operational.sensors import Sensor
from ...events import store as _evstore

if TYPE_CHECKING:
    pass


class GmailSensor(Sensor):
    """Wraps gmail_import.sync_gmail() and emits a summary event per sync."""

    name = "gmail_finance"

    def __init__(self, event_store, finance_engine, creds, llm,
                 account_ids: list[str] | None = None,
                 since: str | None = None,
                 max_messages: int = 200):
        super().__init__(event_store)
        self.fe = finance_engine
        self.creds = creds
        self.llm = llm
        self.account_ids = account_ids  # None = all savings/current accounts
        self.since = since
        self.max_messages = max_messages

    @property
    def authenticated(self) -> bool:
        return self.creds is not None

    def poll(self, since: str | None = None, max_messages: int | None = None):
        """Poll Gmail and emit ``finance.gmail_synced`` if new transactions arrive.

        Returns a list of SyncResult dicts, one per account synced.
        """
        from .gmail_import import sync_gmail as _sync

        since = since or self.since
        max_messages = max_messages or self.max_messages

        accounts = self.fe.list_accounts()
        targets = (
            [a for a in accounts if a["id"] in self.account_ids]
            if self.account_ids
            else [a for a in accounts
                  if a.get("account_type") in ("savings", "current", None, "")]
        )

        total_imported = total_skipped = 0
        results = []

        for acc in targets:
            aid = acc["id"]
            bank = acc.get("bank_name", "Bank")

            row = self.fe.conn.execute(
                "SELECT id FROM accounts"
                " WHERE bank_name=? AND account_type='credit_card' LIMIT 1",
                (bank,)
            ).fetchone()
            cc_aid = row[0] if row else self.fe.add_account(
                nickname=f"{bank} Credit Card",
                bank_name=bank,
                account_type="credit_card",
            )

            r = _sync(self.creds, self.fe, aid, self.llm,
                      since=since, max_messages=max_messages,
                      cc_account_id=cc_aid)
            total_imported += r.imported
            total_skipped += r.skipped
            results.append(r.to_dict())

        if total_imported > 0:
            self.publish(_evstore.FINANCE_GMAIL_SYNCED, {
                "imported": total_imported,
                "skipped": total_skipped,
                "accounts_synced": len(targets),
            })

        return results
