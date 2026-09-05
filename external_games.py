"""Boundary for optional, separately installed game integrations.

No third-party implementation, endpoint, asset, database, or credential belongs
in this repository. Operators may inject adapters into GameHall at runtime.
"""
from __future__ import annotations

from typing import Protocol


class ExternalGameAdapter(Protocol):
    name: str
    description: str

    async def open(self, agent_id: str) -> dict: ...
    async def status(self, agent_id: str) -> dict: ...
    async def action(self, agent_id: str, area: str, command: str) -> dict: ...

