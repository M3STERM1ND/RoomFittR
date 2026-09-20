/**
 * The single-item placer (implementation-plan.md 5.6, 7.1).
 *
 * 7.1 lists "single-item auto-placement on Add" as **synchronous**: the user
 * picks a category, and a pose appears immediately. The full beam search
 * stays in Python, where it has the whole plan to optimise; this is the same
 * candidate generators and the same hard checks restricted to one item, which
 * is all that question needs.
 *
 * Why it is not a call to the server: 5.6 has the user adjusting the result
 * straight afterwards by dragging, and every drag is already validated here.
 * A round trip to place the item and then local validation to move it would
 * be two different answers to the same question -- exactly the split 5.5's
 * parity requirement exists to prevent.
 *
 * The scoring is deliberately simpler than the solver's. The solver is
 * choosing between whole layouts; this is choosing where one thing goes in a
 * room the user is looking at, and "against a wall, out of the way, near
 * where they were looking" is the whole of it.
 */

import {
  TOLERANCE_MM,
  type Footprint,
  type Point2,
  gapBetween,
  protrusion,
  separatingAxisOverlap,
} from "./geometry.ts";
import {
  MIN_CONNECTED_FRACTION,
  type FreeRun,
  type RoomAnalysis,
  cellOf,
  circulationGrid,
  connectedComponent,
  doorStandingPoint,
  wallById,
  wallDirection,
  wallInwardNormal,
  walkableCells,
} from "./room.ts";
import { ruleFor } from "./rules.ts";
import { CEILING_CLEARANCE_MM, type PlacedItem } from "./validator.ts";

/** Positions sampled along a free wall run. Matches the solver's step. */
export const WALL_STEP_MM = 100;
/** Grid step for a free-floating placement. Matches the solver's step. */
export const FLOAT_STEP_MM = 200;

/**
 * How many poses get the expensive circulation check.
 *
 * The same trade the solver makes: every other test is a rectangle
 * comparison, this one floods the room. A pose that scores poorly and also
 * seals the room is not one worth evaluating.
 */
export const CIRCULATION_CHECK_LIMIT = 8;

export interface PlacementRequest {
  readonly category: string;
  readonly widthMm: number;
  readonly depthMm: number;
  readonly heightMm: number;
  /**
   * Where the user was looking, if known -- the centre of the viewport, or
   * where they dropped the item. Ties break towards it, so "Add" puts the
   * thing where they were already attending rather than in a far corner.
   */
  readonly near?: Point2;
}

export interface Placement {
  readonly footprint: Footprint;
  readonly againstWallId: string | null;
  readonly score: number;
}

function rotationFacingInto(
  analysis: RoomAnalysis,
  wallId: string,
): number | null {
  const wall = wallById(analysis, wallId);
  if (!wall) return null;
  const [nx, nz] = wallInwardNormal(wall);
  // An item's front is local -Z (2.4), and `rotateXz` is right-handed about
  // +Y, so the rotation whose front normal equals the inward normal is
  // atan2(-nx, -nz). Derived rather than guessed: the reflexive answer is
  // 180 degrees out and faces every sofa at the wall.
  return ((((Math.atan2(-nx, -nz) * 180) / Math.PI) % 360) + 360) % 360;
}

function againstWallPoses(
  analysis: RoomAnalysis,
  run: FreeRun,
  request: PlacementRequest,
  backGapMm: number,
): Array<{ footprint: Footprint; wallId: string }> {
  const wall = wallById(analysis, run.wallId);
  const rotation = rotationFacingInto(analysis, run.wallId);
  if (!wall || rotation === null) return [];
  if (request.widthMm > run.lengthMm + TOLERANCE_MM) return [];

  const [dx, dz] = wallDirection(wall);
  const [nx, nz] = wallInwardNormal(wall);
  const out = backGapMm + request.depthMm / 2;

  const poses: Array<{ footprint: Footprint; wallId: string }> = [];
  const span = run.lengthMm - request.widthMm;
  const steps = Math.max(Math.floor(span / WALL_STEP_MM), 0);
  for (let step = 0; step <= steps; step += 1) {
    const along = run.startMm + request.widthMm / 2 + step * WALL_STEP_MM;
    if (along + request.widthMm / 2 > run.startMm + run.lengthMm + TOLERANCE_MM)
      break;
    poses.push({
      wallId: run.wallId,
      footprint: {
        centerXMm: wall.start[0] + dx * along + nx * out,
        centerZMm: wall.start[1] + dz * along + nz * out,
        widthMm: request.widthMm,
        depthMm: request.depthMm,
        heightMm: request.heightMm,
        rotationDeg: rotation,
        elevationMm: 0,
      },
    });
  }
  return poses;
}

function floatPoses(
  analysis: RoomAnalysis,
  request: PlacementRequest,
): Array<{ footprint: Footprint; wallId: string | null }> {
  let minX = Infinity;
  let minZ = Infinity;
  let maxX = -Infinity;
  let maxZ = -Infinity;
  for (const [x, z] of analysis.floor) {
    minX = Math.min(minX, x);
    minZ = Math.min(minZ, z);
    maxX = Math.max(maxX, x);
    maxZ = Math.max(maxZ, z);
  }

  const poses: Array<{ footprint: Footprint; wallId: string | null }> = [];
  for (let x = minX + FLOAT_STEP_MM / 2; x <= maxX; x += FLOAT_STEP_MM) {
    for (let z = minZ + FLOAT_STEP_MM / 2; z <= maxZ; z += FLOAT_STEP_MM) {
      for (const rotationDeg of [0, 90, 180, 270]) {
        poses.push({
          wallId: null,
          footprint: {
            centerXMm: x,
            centerZMm: z,
            widthMm: request.widthMm,
            depthMm: request.depthMm,
            heightMm: request.heightMm,
            rotationDeg,
            elevationMm: 0,
          },
        });
      }
    }
  }
  return poses;
}

/** The cheap hard checks, in the order that rejects most poses soonest. */
function isLegal(
  footprint: Footprint,
  analysis: RoomAnalysis,
  existing: PlacedItem[],
  isFloorCovering: boolean,
): boolean {
  if (
    footprint.heightMm >
    analysis.ceilingHeightMm - CEILING_CLEARANCE_MM + TOLERANCE_MM
  ) {
    return false;
  }
  if (protrusion(footprint, analysis.floor) > TOLERANCE_MM) return false;

  if (!isFloorCovering) {
    for (const keepout of analysis.doorKeepouts.values()) {
      if (
        keepout.widthMm > 0 &&
        separatingAxisOverlap(footprint, keepout) > TOLERANCE_MM
      ) {
        return false;
      }
    }
  }
  for (const obstacle of analysis.obstacles) {
    if (separatingAxisOverlap(footprint, obstacle.footprint) > TOLERANCE_MM)
      return false;
  }
  for (const item of existing) {
    const itemCovers = ruleFor(item.category).layer === "floor_covering";
    if (isFloorCovering !== itemCovers) continue;
    if (separatingAxisOverlap(footprint, item.footprint) > TOLERANCE_MM)
      return false;
  }
  return true;
}

function keepsTheRoomWalkable(
  footprint: Footprint,
  analysis: RoomAnalysis,
  existing: PlacedItem[],
  category: string,
): boolean {
  if (ruleFor(category).layer === "floor_covering") return true;

  const blocking = [
    ...existing
      .filter((item) => ruleFor(item.category).layer !== "floor_covering")
      .map((item) => item.footprint),
    ...analysis.obstacles.map((obstacle) => obstacle.footprint),
    footprint,
  ];
  const grid = circulationGrid(blocking, analysis.baseGrid);
  const total = walkableCells(grid);
  if (total === 0) return false;

  const seeds: Array<[number, number]> = [];
  for (const opening of analysis.openings) {
    if (opening.type !== "door" && opening.type !== "passage") continue;
    const wall = wallById(analysis, opening.wallId);
    if (!wall) continue;
    const [x, z] = doorStandingPoint(wall, opening);
    seeds.push(cellOf(grid, x, z));
  }
  if (seeds.length === 0) return true;

  const reachable = connectedComponent(grid, seeds[0]!);
  for (const [row, column] of seeds.slice(1)) {
    if (row < 0 || row >= reachable.length) return false;
    if (!reachable[row]![column]) return false;
  }
  let reached = 0;
  for (const row of reachable) for (const cell of row) if (cell) reached += 1;
  return reached / total >= MIN_CONNECTED_FRACTION;
}

function scorePose(
  footprint: Footprint,
  againstWallId: string | null,
  request: PlacementRequest,
  existing: PlacedItem[],
): number {
  const rule = ruleFor(request.category);
  let score = 0;

  // Against a wall when the category wants that. This is most of the answer:
  // a sofa in the middle of a room is a valid layout and a wrong one.
  const prefersWall =
    rule.placement === "against_wall_preferred" ||
    rule.placement === "headboard_to_wall";
  if (prefersWall && againstWallId !== null) score += 100;

  // Room to breathe, up to a point. Beyond about a metre and a half, more
  // space stops reading as better and starts reading as marooned.
  if (existing.length > 0) {
    const gaps = existing.map((item) => gapBetween(footprint, item.footprint));
    score += Math.min(Math.min(...gaps), 1500) * 0.02;
  }

  // Near where the user was looking. Small, so it breaks ties rather than
  // overriding "against a wall" -- but a new item appearing behind the
  // camera reads as the app ignoring them.
  if (request.near) {
    const distance = Math.hypot(
      footprint.centerXMm - request.near[0],
      footprint.centerZMm - request.near[1],
    );
    score -= distance * 0.004;
  }
  return score;
}

/**
 * Propose where to put one new item.
 *
 * Returns null when nothing legal exists, which the UI should report as
 * "there is no room for this" rather than placing it somewhere invalid and
 * letting the validator complain a moment later.
 */
export function placeOne(
  request: PlacementRequest,
  analysis: RoomAnalysis,
  existing: PlacedItem[],
): Placement | null {
  const rule = ruleFor(request.category);
  const isFloorCovering = rule.layer === "floor_covering";
  const prefersWall =
    rule.placement === "against_wall_preferred" ||
    rule.placement === "headboard_to_wall";

  const candidates: Array<{ footprint: Footprint; wallId: string | null }> = [];
  if (prefersWall) {
    for (const run of analysis.freeRuns) {
      candidates.push(...againstWallPoses(analysis, run, request, 0));
    }
  }
  // Float poses are always generated, not only as a fallback: a room whose
  // walls are full still has a middle, and 5.6 lets the user drag afterwards.
  candidates.push(...floatPoses(analysis, request));

  const legal = candidates.filter(({ footprint }) =>
    isLegal(footprint, analysis, existing, isFloorCovering),
  );
  if (legal.length === 0) return null;

  const scored = legal
    .map((candidate) => ({
      ...candidate,
      score: scorePose(
        candidate.footprint,
        candidate.wallId,
        request,
        existing,
      ),
    }))
    // Deterministic: score, then geometry. Never insertion order, so the same
    // room and the same item give the same answer twice.
    .sort(
      (a, b) =>
        b.score - a.score ||
        a.footprint.centerXMm - b.footprint.centerXMm ||
        a.footprint.centerZMm - b.footprint.centerZMm ||
        a.footprint.rotationDeg - b.footprint.rotationDeg,
    );

  for (const candidate of scored.slice(0, CIRCULATION_CHECK_LIMIT)) {
    if (
      keepsTheRoomWalkable(
        candidate.footprint,
        analysis,
        existing,
        request.category,
      )
    ) {
      return {
        footprint: candidate.footprint,
        againstWallId: candidate.wallId,
        score: candidate.score,
      };
    }
  }
  return null;
}
