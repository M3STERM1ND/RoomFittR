"""Shared helpers for the layout tests.

Rooms come from `fixtures/rooms/`, which Phase 0 hand-authored from real
measurements precisely so Track Product could start before the CV pipeline
existed. Using them here rather than inventing new ones means the validator
is exercised against the same rooms the solver and the web app will see.
"""

from __future__ import annotations

import pytest
from layout_helpers import load_room
from roomfittr_layout.room import RoomAnalysis, analyse


@pytest.fixture(scope="session")
def rectangular() -> RoomAnalysis:
    """4.6 x 3.8 m living room, one door on W3, one window on W1."""
    return analyse(load_room("rectangular-living"))


@pytest.fixture(scope="session")
def bedroom() -> RoomAnalysis:
    return analyse(load_room("small-bedroom"))


@pytest.fixture(scope="session")
def l_shaped() -> RoomAnalysis:
    return analyse(load_room("l-shaped-living"))


@pytest.fixture(scope="session")
def open_plan() -> RoomAnalysis:
    return analyse(load_room("open-plan-boundary"))


@pytest.fixture(scope="session")
def many_openings() -> RoomAnalysis:
    return analyse(load_room("many-openings"))


@pytest.fixture(scope="session")
def narrow() -> RoomAnalysis:
    return analyse(load_room("narrow-room"))
