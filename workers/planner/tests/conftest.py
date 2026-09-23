"""Fixtures for the planner tests.

The rooms are Phase 0's hand-authored `fixtures/rooms/`, the same ones the
layout tests use, so a layout that differs between the deterministic engine
and the LLM path differs because of the model rather than the room.
"""

from __future__ import annotations

import pytest
from planner_helpers import load_room
from roomfittr_layout.room import RoomAnalysis, analyse


@pytest.fixture(scope="session")
def living() -> RoomAnalysis:
    """4.6 x 3.8 m living room, one door on W3, one window on W1."""
    return analyse(load_room("rectangular-living"))


@pytest.fixture(scope="session")
def bedroom() -> RoomAnalysis:
    return analyse(load_room("small-bedroom"))


@pytest.fixture(scope="session")
def narrow() -> RoomAnalysis:
    return analyse(load_room("narrow-room"))
