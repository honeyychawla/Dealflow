"""Channel connectors. Each one turns a source into ingest.Thread objects."""

from __future__ import annotations


class ConnectorError(Exception):
    """A sync could not run or finish. The message is shown to the user as-is."""


class NotConfigured(ConnectorError):
    """The connector has no credentials yet."""
