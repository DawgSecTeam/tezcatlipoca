"""C2 abstraction: Sliver by default (Mythic remains selectable), one interface for everything above it."""

from ..errors import C2Error
from .mythic import DEFAULT_TRANSPORTS, MythicClient  # noqa: F401
from .sliver import (DEFAULT_PORTS, PIVOT_TRANSPORTS, TRANSPORTS,
                     FakeSliver, SliverClient)

__all__ = ["SliverClient", "MythicClient", "C2Error", "DEFAULT_TRANSPORTS",
           "TRANSPORTS", "PIVOT_TRANSPORTS", "DEFAULT_PORTS", "FakeSliver",
           "FakeMythic"]


class FakeMythic(FakeSliver):
    """Backwards-compatible alias: the old in-memory stand-in is the same shape."""
