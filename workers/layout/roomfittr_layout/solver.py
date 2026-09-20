"""L5: the deterministic placement solver (implementation-plan.md 5.4).

The solver is the half of Decision 4 that turns an intent into coordinates.
It is given, per slot, a category, a chosen product's dimensions, and a
spatial intent referring to wall and slot ids -- never coordinates. It
produces exact poses, or it says which slot it could not place and why.

Three properties the plan asks for, and why each matters:

- **Deterministic.** "The same plan + seed gives the same layout" (5.4 step
  5). Without it, "Regenerate" and "undo" are indistinguishable, and the
  human evaluation set in Phase 4 scores a different layout each run.
- **Hard-checked incrementally.** A candidate pose that breaks a hard rule is
  never scored, so the beam never carries an illegal layout forward. The full
  validator still runs at the end (L6); this is the cheap filter, not a
  substitute.
- **It degrades rather than fails.** An unsatisfiable intent relaxes
  (preferred wall -> any wall -> float), then the slot is dropped with a
  sentence the user can read. 5.4 is explicit that a layout is always
  produced.

The scoring weights here are a starting point, not a result. Phase 4's human
evaluation ("would I actually arrange it this way", target mean >= 3.5) is
what tunes them, and it needs a catalog and two people scoring 60 cases.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any

from shapely.geometry import Point, Polygon

from .geometry import TOLERANCE_MM, Footprint, gap_between, protrusion, separating_axis_overlap
from .room import (
    MIN_CONNECTED_FRACTION,
    FreeRun,
    RoomAnalysis,
    circulation_grid,
    connected_component,
    door_standing_point,
)
from .rules import CATEGORY_RULES, CategoryRule
from .validator import CEILING_CLEARANCE_MM, PlacedItem

# 5.4 step 2: sampling steps along a wall and out from an anchor.
WALL_STEP_MM = 100.0
RELATION_STEP_MM = 50.0
FLOAT_STEP_MM = 200.0

# 5.4 step 4: beam width.
BEAM_WIDTH = 5

# Scoring weights. Positive terms reward what 5.4 step 3 lists; the negative
# ones are its penalties. Deliberately coarse: these are a prior, and the
# human eval set is what replaces them with something measured.
W_INTENT = 100.0
W_CLEARANCE = 0.02
W_ALIGNMENT = 40.0
W_WALL_CONTACT = 30.0
W_SOFT_PENALTY = 60.0
W_CROWDING = 0.03

# 5.4 step 3 lists a circulation penalty among the scoring terms. It is
# applied here as a *gate* on the best few poses rather than as a score on
# every pose, for two reasons:
#
# - It is the expensive check. Every other test is a polygon operation on two
#   rectangles; this one rasterises the room and floods it.
# - A layout that cuts the room in half is not slightly worse, it is illegal
#   (H6). Trading it off against alignment would let a good-looking score
#   outvote a room the user cannot walk across.
#
# Checking only the top-scoring poses bounds the cost: a pose that scores
# poorly and also blocks the room is not one we needed to evaluate.
CIRCULATION_CHECK_LIMIT = 12


@dataclass(frozen=True, slots=True)
class Candidate:
    """A product the planner picked for a slot, with the size that matters."""

    product_id: str
    width_mm: float
    depth_mm: float
    height_mm: float
    price_cents: int = 0

    @property
    def footprint_area_mm2(self) -> float:
        return self.width_mm * self.depth_mm


@dataclass(frozen=True, slots=True)
class Intent:
    """Where the planner wants a slot, in ids rather than coordinates (5.4).

    `anchor_wall` and `anchor_slot` are the only spatial inputs the LLM
    supplies. Validating that they name things that exist is the planner's
    job; the solver treats an unknown id as "no preference" and relaxes.
    """

    relation: str = "auto"
    anchor_wall: str | None = None
    anchor_slot: str | None = None
    facing: str | None = None


@dataclass(frozen=True, slots=True)
class Slot:
    """One thing to place."""

    slot_id: str
    category: str
    priority: str = "should"
    intent: Intent = field(default_factory=Intent)
    # Ranked shortlist (5.4 L3/L4). The solver walks it when the first choice
    # does not fit, smaller footprints first among the remainder.
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def rule(self) -> CategoryRule:
        return CATEGORY_RULES.get(self.category, CategoryRule())


@dataclass(frozen=True, slots=True)
class Placement:
    """A solved slot."""

    slot_id: str
    item: PlacedItem
    score: float
    relaxed: bool = False


@dataclass(frozen=True, slots=True)
class DroppedSlot:
    """A slot that could not be placed, with a sentence for the user."""

    slot_id: str
    category: str
    reason: str


@dataclass(frozen=True, slots=True)
class SolverResult:
    placements: list[Placement]
    dropped: list[DroppedSlot]
    seed: int

    @property
    def items(self) -> list[PlacedItem]:
        return [placement.item for placement in self.placements]

    def as_meta(self) -> dict[str, Any]:
        """The `generation_meta` fragment for the layouts row (6.2)."""
        return {
            "seed": self.seed,
            "placed": len(self.placements),
            "dropped": [
                {"slot_id": d.slot_id, "category": d.category, "reason": d.reason}
                for d in self.dropped
            ],
            "relaxed": [p.slot_id for p in self.placements if p.relaxed],
        }


def _wall_facing_rotation(analysis: RoomAnalysis, wall_id: str) -> float | None:
    """The rotation that puts an item's back to `wall_id` and its front inward.

    An item's front is local -Z (2.4), and `rotate_xz` is right-handed about
    +Y, so the rotation whose front normal equals the wall's inward normal is
    `atan2(-nx, -nz)` -- derived rather than guessed, because the reflexive
    answer is out by 180 degrees and produces a room full of sofas facing
    the wall.
    """
    wall = analysis.wall(wall_id)
    if wall is None:
        return None
    nx, nz = wall.inward_normal()
    return math.degrees(math.atan2(-nx, -nz)) % 360.0


def _against_wall_poses(
    analysis: RoomAnalysis, run: FreeRun, candidate: Candidate, rule: CategoryRule
) -> list[Footprint]:
    """Positions along one free run, back flush with the wall plus its gap."""
    wall = analysis.wall(run.wall_id)
    rotation = _wall_facing_rotation(analysis, run.wall_id)
    if wall is None or rotation is None:
        return []
    if candidate.width_mm > run.length_mm + TOLERANCE_MM:
        return []

    gap = rule.back_to_wall_gap_mm[0]
    nx, nz = wall.inward_normal()
    dx, dz = wall.direction()

    poses: list[Footprint] = []
    span = run.length_mm - candidate.width_mm
    steps = max(int(span // WALL_STEP_MM), 0)
    for step in range(steps + 1):
        along = run.start_mm + candidate.width_mm / 2.0 + step * WALL_STEP_MM
        if along + candidate.width_mm / 2.0 > run.end_mm + TOLERANCE_MM:
            break
        out = gap + candidate.depth_mm / 2.0
        poses.append(
            Footprint(
                center_x_mm=wall.start[0] + dx * along + nx * out,
                center_z_mm=wall.start[1] + dz * along + nz * out,
                width_mm=candidate.width_mm,
                depth_mm=candidate.depth_mm,
                height_mm=candidate.height_mm,
                rotation_deg=rotation,
            )
        )
    return poses


def _in_front_of_poses(
    anchor: PlacedItem, candidate: Candidate, rule: CategoryRule
) -> list[Footprint]:
    """Centred on the anchor's front axis, at each gap in the rule's band."""
    fx, fz = anchor.footprint.front_normal()
    low, high = rule.gap_mm if rule.gap_mm != (0.0, 0.0) else (350.0, 500.0)

    poses: list[Footprint] = []
    gap = low
    while gap <= high + 1e-6:
        out = anchor.footprint.depth_mm / 2.0 + gap + candidate.depth_mm / 2.0
        poses.append(
            Footprint(
                center_x_mm=anchor.footprint.center_x_mm + fx * out,
                center_z_mm=anchor.footprint.center_z_mm + fz * out,
                width_mm=candidate.width_mm,
                depth_mm=candidate.depth_mm,
                height_mm=candidate.height_mm,
                # Square to the anchor, so a coffee table lines up with its
                # sofa rather than sitting at an angle to it.
                rotation_deg=anchor.footprint.rotation_deg,
            )
        )
        gap += RELATION_STEP_MM
    return poses


def _beside_poses(anchor: PlacedItem, candidate: Candidate, rule: CategoryRule) -> list[Footprint]:
    """To either side of the anchor, touching or within the rule's gap."""
    # The anchor's local +X in world terms, from its front normal rotated 90.
    fx, fz = anchor.footprint.front_normal()
    sx, sz = -fz, fx

    low, high = rule.gap_mm
    poses: list[Footprint] = []
    gap = low
    while gap <= max(high, low) + 1e-6:
        out = anchor.footprint.width_mm / 2.0 + gap + candidate.width_mm / 2.0
        for sign in (1.0, -1.0):
            poses.append(
                Footprint(
                    center_x_mm=anchor.footprint.center_x_mm + sx * out * sign,
                    center_z_mm=anchor.footprint.center_z_mm + sz * out * sign,
                    width_mm=candidate.width_mm,
                    depth_mm=candidate.depth_mm,
                    height_mm=candidate.height_mm,
                    rotation_deg=anchor.footprint.rotation_deg,
                )
            )
        gap += RELATION_STEP_MM
    return poses


def _under_poses(anchor: PlacedItem, candidate: Candidate) -> list[Footprint]:
    """A floor covering under an anchor.

    Offset forwards as well as centred: 5.3's rug rule wants a rug under a
    sofa's front legs rather than centred on it, and which reads better
    depends on the room.
    """
    fx, fz = anchor.footprint.front_normal()
    poses: list[Footprint] = []
    for forward in (0.0, candidate.depth_mm * 0.25, candidate.depth_mm * 0.4):
        poses.append(
            Footprint(
                center_x_mm=anchor.footprint.center_x_mm + fx * forward,
                center_z_mm=anchor.footprint.center_z_mm + fz * forward,
                width_mm=candidate.width_mm,
                depth_mm=candidate.depth_mm,
                height_mm=candidate.height_mm,
                rotation_deg=anchor.footprint.rotation_deg,
            )
        )
    return poses


def _around_poses(anchor: PlacedItem, candidate: Candidate, rule: CategoryRule) -> list[Footprint]:
    """Chairs spaced around a table (5.4)."""
    pullback = rule.pullback_mm or 700.0
    fx, fz = anchor.footprint.front_normal()
    sx, sz = -fz, fx

    poses: list[Footprint] = []
    for dx, dz, rotation_offset in (
        (fx, fz, 0.0),
        (-fx, -fz, 180.0),
        (sx, sz, 270.0),
        (-sx, -sz, 90.0),
    ):
        reach = (
            anchor.footprint.depth_mm / 2.0
            if abs(dx * fx + dz * fz) > 0.5
            else anchor.footprint.width_mm / 2.0
        )
        out = reach + pullback * 0.35 + candidate.depth_mm / 2.0
        poses.append(
            Footprint(
                center_x_mm=anchor.footprint.center_x_mm + dx * out,
                center_z_mm=anchor.footprint.center_z_mm + dz * out,
                width_mm=candidate.width_mm,
                depth_mm=candidate.depth_mm,
                height_mm=candidate.height_mm,
                rotation_deg=(anchor.footprint.rotation_deg + rotation_offset) % 360.0,
            )
        )
    return poses


def _float_poses(analysis: RoomAnalysis, candidate: Candidate) -> list[Footprint]:
    """A grid over the usable floor, four rotations each (5.4)."""
    region = analysis.usable_floor
    min_x, min_z, max_x, max_z = region.bounds

    poses: list[Footprint] = []
    x = min_x + FLOAT_STEP_MM / 2.0
    while x <= max_x:
        z = min_z + FLOAT_STEP_MM / 2.0
        while z <= max_z:
            if region.contains(Point(x, z)):
                for rotation in (0.0, 90.0, 180.0, 270.0):
                    poses.append(
                        Footprint(
                            center_x_mm=x,
                            center_z_mm=z,
                            width_mm=candidate.width_mm,
                            depth_mm=candidate.depth_mm,
                            height_mm=candidate.height_mm,
                            rotation_deg=rotation,
                        )
                    )
            z += FLOAT_STEP_MM
        x += FLOAT_STEP_MM
    return poses


def candidate_poses(
    slot: Slot,
    candidate: Candidate,
    analysis: RoomAnalysis,
    placed: dict[str, PlacedItem],
    *,
    relaxation: int = 0,
) -> tuple[list[Footprint], bool]:
    """Poses for one product in one slot, at a given relaxation level.

    5.4 step 2's relaxation ladder: the preferred wall, then any wall, then
    float anywhere. Returns the poses and whether the intent was honoured, so
    scoring can prefer an un-relaxed placement without the solver refusing a
    relaxed one.
    """
    rule = slot.rule
    anchor = placed.get(slot.intent.anchor_slot or "")

    if relaxation == 0:
        if anchor is not None and rule.relation:
            by_relation = {
                "in_front_of": lambda: _in_front_of_poses(anchor, candidate, rule),
                "beside": lambda: _beside_poses(anchor, candidate, rule),
                "under": lambda: _under_poses(anchor, candidate),
                "around": lambda: _around_poses(anchor, candidate, rule),
                "at": lambda: _in_front_of_poses(anchor, candidate, rule),
            }
            builder = by_relation.get(rule.relation)
            if builder is not None:
                poses = builder()
                if poses:
                    return poses, True

        if slot.intent.anchor_wall:
            poses = [
                pose
                for run in analysis.runs_for(slot.intent.anchor_wall)
                for pose in _against_wall_poses(analysis, run, candidate, rule)
            ]
            if poses:
                return poses, True

        if rule.prefers_wall:
            poses = [
                pose
                for run in analysis.free_runs
                for pose in _against_wall_poses(analysis, run, candidate, rule)
            ]
            if poses:
                return poses, True

    if relaxation <= 1 and rule.prefers_wall:
        poses = [
            pose
            for run in analysis.free_runs
            for pose in _against_wall_poses(analysis, run, candidate, rule)
        ]
        if poses:
            return poses, False

    return _float_poses(analysis, candidate), relaxation == 0 and not rule.prefers_wall


def circulation_ok(
    analysis: RoomAnalysis,
    items: list[PlacedItem],
    *,
    floor_area_floor: float = MIN_CONNECTED_FRACTION,
) -> bool:
    """Whether H6 still holds with `items` in place.

    The same computation the validator performs, exposed so the solver can
    refuse a pose that seals the room rather than discovering it in L6 and
    having to drop a slot. A narrow room is where this matters: a coffee
    table in the middle of a 1.9 m-deep room leaves 650 mm either side, and
    650 mm is not a walkway.
    """
    blocking = [i.footprint for i in items if not i.is_floor_covering] + [
        o.footprint for o in analysis.obstacles
    ]
    grid = circulation_grid(analysis.floor, blocking, base=analysis.base_grid)
    total = grid.walkable_cells
    if total == 0:
        return False

    seeds = []
    for opening in analysis.openings:
        if opening.type not in ("door", "passage"):
            continue
        wall = analysis.wall(opening.wall_id)
        if wall is None:
            continue
        x, z = door_standing_point(wall, opening)
        seeds.append(grid.cell_of(x, z))

    if not seeds:
        # No door to enter by. Fall back to the largest region, which is what
        # the validator measures in the same situation.
        rows, columns = grid.walkable.shape
        best = 0
        seen = set()
        for row in range(rows):
            for column in range(columns):
                if grid.walkable[row, column] and (row, column) not in seen:
                    region = connected_component(grid, (row, column))
                    seen |= {(int(r), int(c)) for r, c in zip(*region.nonzero(), strict=False)}
                    best = max(best, int(region.sum()))
        return best / total >= floor_area_floor

    reachable = connected_component(grid, seeds[0])
    rows, columns = grid.walkable.shape
    for cell in seeds[1:]:
        if not (0 <= cell[0] < rows and 0 <= cell[1] < columns) or not reachable[cell]:
            return False
    return int(reachable.sum()) / total >= floor_area_floor


def _hard_ok(
    footprint: Footprint, analysis: RoomAnalysis, placed: list[PlacedItem], rule: CategoryRule
) -> bool:
    """The cheap incremental hard check of 5.4 step 3.

    Not a substitute for L6: circulation and budget are global and are checked
    once at the end. This is the filter that stops the beam carrying an
    obviously illegal pose forward.

    Ordered cheapest-first, and it matters: a float intent generates a grid of
    positions times four rotations, so this runs tens of thousands of times
    per layout. Height is a float comparison, the bounding-box test is four
    numpy comparisons, and only what survives both is turned into a shapely
    polygon.
    """
    if footprint.top_mm > analysis.ceiling_height_mm - CEILING_CLEARANCE_MM + TOLERANCE_MM:
        return False

    corners = footprint.corners()
    min_x, min_z, max_x, max_z = analysis.floor.bounds
    if (
        corners[:, 0].min() < min_x - TOLERANCE_MM
        or corners[:, 0].max() > max_x + TOLERANCE_MM
        or corners[:, 1].min() < min_z - TOLERANCE_MM
        or corners[:, 1].max() > max_z + TOLERANCE_MM
    ):
        return False

    shape = Polygon(corners)
    if not analysis.floor.covers(shape) and protrusion(footprint, analysis.floor) > TOLERANCE_MM:
        return False

    if not rule.is_floor_covering:
        for keepout in analysis.door_keepouts.values():
            if keepout.width_mm > 0 and separating_axis_overlap(footprint, keepout) > TOLERANCE_MM:
                return False
    for obstacle in analysis.obstacles:
        if (
            footprint.height_overlaps(obstacle.footprint)
            and separating_axis_overlap(footprint, obstacle.footprint) > TOLERANCE_MM
        ):
            return False
    for other in placed:
        if rule.is_floor_covering or other.is_floor_covering:
            continue
        if (
            footprint.height_overlaps(other.footprint)
            and separating_axis_overlap(footprint, other.footprint) > TOLERANCE_MM
        ):
            return False
    return True


def score_pose(
    footprint: Footprint,
    slot: Slot,
    analysis: RoomAnalysis,
    placed: list[PlacedItem],
    *,
    intent_met: bool,
) -> float:
    """5.4 step 3's scoring. Higher is better."""
    rule = slot.rule
    score = W_INTENT if intent_met else 0.0

    # Reward being properly against a wall when the category wants that.
    if rule.prefers_wall:
        nearest = min(
            (
                footprint.polygon().distance(
                    Point(wall.point_at(wall.length_mm / 2.0))
                    .buffer(0)
                    .union(Point(wall.start).buffer(0))
                )
                for wall in analysis.walls
                if wall.kind == "solid"
            ),
            default=1e9,
        )
        score += W_WALL_CONTACT * (1.0 if nearest < 1e8 else 0.0)

    # Clearance margin: more space around an item is better, up to a point.
    if placed:
        gaps = [gap_between(footprint, other.footprint) for other in placed]
        tightest = min(gaps)
        score += W_CLEARANCE * min(tightest, 1500.0)
        # ...but hugging a corner of the room while everything else huddles
        # elsewhere is not better, so penalise being far from the group.
        spread = sum(gaps) / len(gaps)
        score -= W_CROWDING * max(spread - 2500.0, 0.0)

    # Alignment: centred on its wall run reads as deliberate.
    if rule.prefers_wall:
        for run in analysis.free_runs:
            wall = analysis.wall(run.wall_id)
            if wall is None:
                continue
            centre = wall.point_at(run.start_mm + run.length_mm / 2.0)
            offset = math.dist((footprint.center_x_mm, footprint.center_z_mm), centre)
            if offset < run.length_mm:
                score += W_ALIGNMENT * max(0.0, 1.0 - offset / max(run.length_mm, 1.0))
                break

    # Soft penalties the validator would raise.
    for opening_id, zone in analysis.window_zones.items():
        opening = next((o for o in analysis.openings if o.id == opening_id), None)
        if opening is None or zone.width_mm <= 0:
            continue
        if (
            footprint.top_mm > opening.sill_mm - 50.0
            and separating_axis_overlap(footprint, zone) > TOLERANCE_MM
        ):
            score -= W_SOFT_PENALTY
    return score


def solve(
    slots: list[Slot],
    analysis: RoomAnalysis,
    *,
    seed: int = 0,
    beam_width: int = BEAM_WIDTH,
) -> SolverResult:
    """Place every slot, in dependency order, by beam search (5.4 step 4).

    The beam carries whole partial layouts rather than per-slot choices,
    because a pose is only good relative to what is already down: the best
    sofa position depends on nothing, the best coffee-table position depends
    entirely on the sofa.
    """
    ordered = _dependency_order(slots)

    beams: list[tuple[float, list[Placement]]] = [(0.0, [])]
    dropped: list[DroppedSlot] = []

    for slot in ordered:
        next_beams: list[tuple[float, list[Placement]]] = []
        failure: str | None = None

        for running_score, partial in beams:
            placed_by_slot = {p.slot_id: p.item for p in partial}
            placed_items = [p.item for p in partial]

            found = False
            for candidate in _candidate_order(slot):
                for relaxation in (0, 1, 2):
                    poses, intent_met = candidate_poses(
                        slot, candidate, analysis, placed_by_slot, relaxation=relaxation
                    )
                    scored: list[tuple[float, Footprint]] = []
                    for pose in poses:
                        if not _hard_ok(pose, analysis, placed_items, slot.rule):
                            continue
                        scored.append(
                            (
                                score_pose(
                                    pose, slot, analysis, placed_items, intent_met=intent_met
                                ),
                                pose,
                            )
                        )
                    if not scored:
                        continue

                    # Deterministic: sort by score, then by geometry, never by
                    # insertion order or by anything a set or dict decides.
                    scored.sort(
                        key=lambda pair: (
                            -pair[0],
                            round(pair[1].center_x_mm, 3),
                            round(pair[1].center_z_mm, 3),
                            round(pair[1].rotation_deg, 3),
                        )
                    )
                    kept = 0
                    for pose_score, pose in scored[:CIRCULATION_CHECK_LIMIT]:
                        item = PlacedItem(
                            id=slot.slot_id,
                            category=slot.category,
                            footprint=pose,
                            price_cents=candidate.price_cents,
                            against_wall_id=(
                                slot.intent.anchor_wall if slot.rule.prefers_wall else None
                            ),
                        )
                        if not circulation_ok(analysis, [*placed_items, item]):
                            continue
                        next_beams.append(
                            (
                                running_score + pose_score,
                                [
                                    *partial,
                                    Placement(
                                        slot_id=slot.slot_id,
                                        item=item,
                                        score=pose_score,
                                        relaxed=relaxation > 0,
                                    ),
                                ],
                            )
                        )
                        kept += 1
                        if kept >= beam_width:
                            break
                    if kept == 0:
                        continue
                    found = True
                    break
                if found:
                    break

            if not found and failure is None:
                failure = _failure_reason(slot, analysis)

        if not next_beams:
            if slot.priority == "must":
                # 7.4: the whole layout fails rather than quietly shipping a
                # bedroom with no bed.
                raise SolverError(
                    "ROOM_TOO_SMALL_FOR_BUDGET_OR_TYPE",
                    failure or f"there is no room for the {slot.category.replace('_', ' ')}",
                )
            dropped.append(
                DroppedSlot(
                    slot_id=slot.slot_id,
                    category=slot.category,
                    reason=failure or f"No room for the {slot.category.replace('_', ' ')}.",
                )
            )
            continue

        next_beams.sort(key=lambda pair: (-pair[0], [p.slot_id for p in pair[1]]))
        beams = next_beams[:beam_width]

    best = max(beams, key=lambda pair: pair[0]) if beams else (0.0, [])
    return SolverResult(placements=best[1], dropped=dropped, seed=seed)


class SolverError(RuntimeError):
    """A `must` slot could not be placed (7.4's layout failure)."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _dependency_order(slots: list[Slot]) -> list[Slot]:
    """Anchors, then dependents, then floor coverings (5.4 step 1).

    A dependent whose anchor is not in the plan is demoted to an anchor: it
    has nothing to be relative to, so it may as well be placed first and
    freely, rather than relaxed into a float after the room has filled up.
    """
    present = {slot.category for slot in slots}

    def rank(slot: Slot) -> tuple[int, str]:
        rule = slot.rule
        if rule.is_floor_covering:
            return (2, slot.slot_id)
        if rule.relation and (set(rule.anchor_categories) & present):
            return (1, slot.slot_id)
        return (0, slot.slot_id)

    return sorted(slots, key=rank)


def _candidate_order(slot: Slot) -> list[Candidate]:
    """The shortlist, first choice first, then smallest footprint (5.4 step 4).

    "If a slot has no valid pose, try the next product in its shortlist,
    smaller footprint first" -- the point being that the reason a slot failed
    is almost always size, so trying a bigger alternative next wastes the
    attempt.
    """
    if not slot.candidates:
        return []
    head, *rest = slot.candidates
    return [head, *sorted(rest, key=lambda c: c.footprint_area_mm2)]


def _failure_reason(slot: Slot, analysis: RoomAnalysis) -> str:
    """A sentence a person can act on, per 7.4's layout-failure guidance."""
    label = slot.category.replace("_", " ")
    if not slot.candidates:
        return f"We don't have a {label} in the catalog that fits your budget."

    smallest = min(c.width_mm for c in slot.candidates)
    longest_run = max((run.length_mm for run in analysis.free_runs), default=0.0)
    if slot.rule.prefers_wall and longest_run > 0 and smallest > longest_run:
        return (
            f"The narrowest {label} we could find is {int(smallest)} mm wide and the "
            f"longest clear wall is {int(longest_run)} mm."
        )
    return f"There was no space left for a {label} that keeps the doorway clear."


def with_locked(result: SolverResult, locked: list[PlacedItem]) -> SolverResult:
    """Re-attach items the user pinned before a regenerate (7.2).

    Locked items are placed into the analysis as obstacles by the caller, so
    by the time the solver runs they are already respected. This just folds
    them back into the result so the layout is complete.
    """
    pinned = [Placement(slot_id=item.id, item=item, score=0.0, relaxed=False) for item in locked]
    return replace(result, placements=[*pinned, *result.placements])
