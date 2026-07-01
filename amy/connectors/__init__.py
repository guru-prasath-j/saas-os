"""Connectors: pluggable access to email / calendar / tasks (private mode only).

    from amy.connectors import ConnectorRegistry
    reg = ConnectorRegistry("/data/connectors/<user>")
    reg.list("email", mode="private")     # ok
    reg.list("email", mode="public")      # PermissionError (blocked)

Real providers (Gmail, Google Calendar/Tasks) implement amy.connectors.base.Connector
and are registered with reg.register(kind, provider).
"""
from .base import Connector, Item
from .local import LocalEmailProvider, LocalCalendarProvider, LocalTaskProvider
from .registry import ConnectorRegistry
