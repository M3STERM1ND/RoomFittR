/**
 * The deterministic validator, in the browser (implementation-plan.md 5.5).
 *
 * The TypeScript twin of `workers/layout/roomfittr_layout/validator.py`. 5.5
 * requires the two to produce identical reports, and `fixtures/validation/`
 * pins that in CI. The server uses the Python one on save; this one runs on
 * every pointer-move while dragging, so a user never builds something the
 * server will then reject.
 *
 * Identical means identical JSON: the same codes, the same severities, the
 * same sentences, the same measured integers, in the same order. Two places
 * needed care to get there, and both are marked in the code:
 *
 * - **Rounding.** Python's `round()` is half-to-even; JavaScript's
 *   `Math.round` is half-up. `mm()` below implements Python's rule.
 * - **Percentages and money** are computed by integer division on both
 *   sides, so no float ever reaches a format string.
 */

import {
  TOLERANCE_MM,
  type Footprint,
  frontNormal as frontNormalOf,
  gapBetween,
  heightOverlaps,
  protrusion,
  separatingAxisOverlap,
  topMm,
} from "./geometry.ts";
import {
  MIN_CONNECTED_FRACTION,
  type RoomAnalysis,
  cellOf,
  circulationGrid,
  connectedComponent,
  doorStandingPoint,
  runsFor,
  wallById,
  wallInwardNormal,
  walkableCells,
} from "./room.ts";
import { type CategoryRule, ruleFor } from "./rules.ts";

export const SCHEMA_VERSION = 1;

/** H6 / S1: the minimum and the comfortable walkway. */
export const MIN_WALKWAY_MM = 760;
export const COMFORTABLE_WALKWAY_MM = 900;
/** H5: items must clear the ceiling by this much. */
export const CEILING_CLEARANCE_MM = 50;
/** S5: thresholds for an item out of proportion with its wall or its room. */
export const MAX_WALL_RUN_FRACTION = 0.7;
export const MAX_ROOM_FRACTION = 0.9;
/** S6: a price older than this is shown with a caveat. */
export const STALE_PRICE_DAYS = 7;

export interface PlacedItem {
  readonly id: string;
  readonly category: string;
  readonly footprint: Footprint;
  readonly priceCents: number;
  readonly againstWallId: string | null;
  readonly priceAgeDays: number;
  readonly available: boolean;
}

export function placedItem(
  partial: Partial<PlacedItem> &
    Pick<PlacedItem, "id" | "category" | "footprint">,
): PlacedItem {
  return {
    priceCents: 0,
    againstWallId: null,
    priceAgeDays: 0,
    available: true,
    ...partial,
  };
}

export interface Violation {
  readonly code: string;
  readonly severity: "hard" | "soft";
  readonly message: string;
  readonly measuredMm?: number;
  readonly limitMm?: number;
  readonly relatedIds?: string[];
}

export interface ValidationReport {
  readonly fits: boolean;
  readonly violations: Violation[];
  readonly items: Map<string, Violation[]>;
  readonly minWalkwayMm: number | null;
  readonly freeFloorPct: number | null;
  readonly totalPriceCents: number;
}

/**
 * Python's `round()`: half-to-even, not half-up.
 *
 * `Math.round(0.5)` is 1 and `Math.round(-0.5)` is -0, while Python gives 0
 * for both. Every measured value in a report goes through here, so an item
 * overlapping by exactly 2.5 mm reports the same integer on both sides.
 */
export function mm(value: number): number {
  const floor = Math.floor(value);
  const remainder = value - floor;
  if (remainder > 0.5) return floor + 1;
  if (remainder < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

function isFloorCovering(item: PlacedItem): boolean {
  return ruleFor(item.category).layer === "floor_covering";
}

/** The category as a person would say it: `coffee_table` -> `coffee table`. */
function label(item: PlacedItem): string {
  return item.category.replaceAll("_", " ");
}

function joinDoors(ids: string[]): string {
  if (ids.length === 1) return `the ${ids[0]} doorway`;
  return `the ${ids.slice(0, -1).join(", ")} and ${ids[ids.length - 1]} doorways`;
}

/** Python's `f"{n:,}"` for an integer. */
function withThousands(value: number): string {
  return value.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

export interface ValidateOptions {
  readonly budgetCents?: number | null;
  readonly enforceBudget?: boolean;
}

/**
 * Run every rule.
 *
 * `enforceBudget: false` is for manually edited layouts: 5.5 H7 applies to
 * AI layouts only, and a user who deliberately adds an expensive sofa should
 * see "over budget" rather than be blocked.
 */
export function validate(
  items: PlacedItem[],
  analysis: RoomAnalysis,
  options: ValidateOptions = {},
): ValidationReport {
  const budgetCents = options.budgetCents ?? null;
  const enforceBudget = options.enforceBudget ?? true;

  const violations: Violation[] = [];
  const perItem = new Map<string, Violation[]>();
  for (const item of items) perItem.set(item.id, []);
  const push = (id: string, violation: Violation): void => {
    perItem.get(id)!.push(violation);
  };

  checkContainment(items, analysis, push);
  checkItemOverlaps(items, push);
  checkObstacles(items, analysis, push);
  checkDoorKeepouts(items, analysis, push);
  checkHeight(items, analysis, push);
  checkWindows(items, analysis, push);
  checkWallFacing(items, analysis, push);
  checkOversized(items, analysis, push);
  checkAvailability(items, push);
  const minGap = checkCategoryClearances(items, push);

  const { connectedPct, blockedDoors } = checkCirculation(items, analysis);
  if (blockedDoors.length > 0) {
    violations.push({
      code: "H6_CIRCULATION_BLOCKED",
      severity: "hard",
      message: `You couldn't walk from ${joinDoors(blockedDoors)} to the rest of the room.`,
      relatedIds: blockedDoors,
    });
  } else if (connectedPct < Math.trunc(MIN_CONNECTED_FRACTION * 100)) {
    violations.push({
      code: "H6_CIRCULATION_BLOCKED",
      severity: "hard",
      message:
        `This layout cuts the room up: only ${connectedPct}% of the ` +
        "open floor can be reached in one piece.",
      measuredMm: connectedPct,
      limitMm: Math.trunc(MIN_CONNECTED_FRACTION * 100),
    });
  }

  const totalPrice = items.reduce((sum, item) => sum + item.priceCents, 0);
  if (budgetCents !== null && totalPrice > budgetCents) {
    const overDollars = Math.floor((totalPrice - budgetCents) / 100);
    violations.push({
      code: "H7_OVER_BUDGET",
      severity: enforceBudget ? "hard" : "soft",
      message: `This layout is $${withThousands(overDollars)} over your budget.`,
    });
  }

  if (minGap !== null && minGap < COMFORTABLE_WALKWAY_MM) {
    violations.push({
      code: "S1_WALKWAY_TIGHT",
      severity: "soft",
      message:
        `The tightest gap between furniture is ${mm(minGap)} mm. ` +
        `${COMFORTABLE_WALKWAY_MM} mm is more comfortable to walk through.`,
      measuredMm: mm(minGap),
      limitMm: COMFORTABLE_WALKWAY_MM,
    });
  }

  const everything = [...violations, ...[...perItem.values()].flat()];
  return {
    fits: !everything.some((v) => v.severity === "hard"),
    violations,
    items: perItem,
    minWalkwayMm: minGap === null ? null : mm(minGap),
    freeFloorPct: connectedPct,
    totalPriceCents: totalPrice,
  };
}

type Push = (id: string, violation: Violation) => void;

/** H1: the whole footprint inside the floor polygon, within 5 mm. */
function checkContainment(
  items: PlacedItem[],
  analysis: RoomAnalysis,
  push: Push,
): void {
  for (const item of items) {
    const out = protrusion(item.footprint, analysis.floor);
    if (out > TOLERANCE_MM) {
      push(item.id, {
        code: "H1_OUTSIDE_FLOOR",
        severity: "hard",
        message: `The ${label(item)} sticks ${mm(out)} mm outside the room.`,
        measuredMm: mm(out),
        limitMm: 0,
      });
    }
  }
}

/**
 * H2: no 3D overlap, except that anything may sit on a floor covering.
 *
 * Two rugs may not overlap each other, which is why the exception is "exactly
 * one of them is a floor covering" rather than "either is".
 */
function checkItemOverlaps(items: PlacedItem[], push: Push): void {
  for (let i = 0; i < items.length; i += 1) {
    for (let j = i + 1; j < items.length; j += 1) {
      const first = items[i]!;
      const second = items[j]!;
      const firstCovers = isFloorCovering(first);
      const secondCovers = isFloorCovering(second);
      if (firstCovers !== secondCovers) continue;
      if (!heightOverlaps(first.footprint, second.footprint)) continue;

      const overlap = separatingAxisOverlap(first.footprint, second.footprint);
      if (overlap <= TOLERANCE_MM) continue;

      const message = `The ${label(first)} and the ${label(second)} overlap by ${mm(overlap)} mm.`;
      push(first.id, {
        code: "H2_ITEM_OVERLAP",
        severity: "hard",
        message,
        measuredMm: mm(overlap),
        limitMm: 0,
        relatedIds: [second.id],
      });
      push(second.id, {
        code: "H2_ITEM_OVERLAP",
        severity: "hard",
        message,
        measuredMm: mm(overlap),
        limitMm: 0,
        relatedIds: [first.id],
      });
    }
  }
}

/** H3: nothing overlaps a fixed obstacle or an object the user kept. */
function checkObstacles(
  items: PlacedItem[],
  analysis: RoomAnalysis,
  push: Push,
): void {
  for (const item of items) {
    for (const obstacle of analysis.obstacles) {
      if (!heightOverlaps(item.footprint, obstacle.footprint)) continue;
      const overlap = separatingAxisOverlap(item.footprint, obstacle.footprint);
      if (overlap <= TOLERANCE_MM) continue;
      push(item.id, {
        code: "H3_OBSTACLE_OVERLAP",
        severity: "hard",
        message: `The ${label(item)} runs ${mm(overlap)} mm into the ${obstacle.label}.`,
        measuredMm: mm(overlap),
        limitMm: 0,
        relatedIds: [obstacle.id],
      });
    }
  }
}

/** H4: nothing but a floor covering inside a door's swing and approach. */
function checkDoorKeepouts(
  items: PlacedItem[],
  analysis: RoomAnalysis,
  push: Push,
): void {
  for (const item of items) {
    if (isFloorCovering(item)) continue;
    for (const [openingId, keepout] of analysis.doorKeepouts) {
      if (keepout.widthMm <= 0) continue;
      if (separatingAxisOverlap(item.footprint, keepout) <= TOLERANCE_MM)
        continue;
      push(item.id, {
        code: "H4_DOOR_KEEPOUT",
        severity: "hard",
        message: `The ${label(item)} is in the way of the door.`,
        relatedIds: [openingId],
      });
    }
  }
}

/** H5: height at most the ceiling less 50 mm. */
function checkHeight(
  items: PlacedItem[],
  analysis: RoomAnalysis,
  push: Push,
): void {
  const limit = analysis.ceilingHeightMm - CEILING_CLEARANCE_MM;
  for (const item of items) {
    if (topMm(item.footprint) > limit + TOLERANCE_MM) {
      push(item.id, {
        code: "H5_TOO_TALL",
        severity: "hard",
        message:
          `The ${label(item)} is ${mm(topMm(item.footprint))} mm tall ` +
          `and the ceiling is ${mm(analysis.ceilingHeightMm)} mm.`,
        measuredMm: mm(topMm(item.footprint)),
        limitMm: mm(limit),
      });
    }
  }
}

/**
 * S3: a tall item standing in a window's zone blocks the view.
 *
 * Height is measured against the sill, so a sideboard under a high window is
 * fine and a bookcase in front of it is not.
 */
function checkWindows(
  items: PlacedItem[],
  analysis: RoomAnalysis,
  push: Push,
): void {
  const byId = new Map(
    analysis.openings.map((opening) => [opening.id, opening]),
  );
  for (const item of items) {
    for (const [openingId, zone] of analysis.windowZones) {
      const opening = byId.get(openingId);
      if (!opening || zone.widthMm <= 0) continue;
      if (topMm(item.footprint) <= opening.sillMm - 50) continue;
      if (separatingAxisOverlap(item.footprint, zone) <= TOLERANCE_MM) continue;
      push(item.id, {
        code: "S3_WINDOW_BLOCKED",
        severity: "soft",
        message: `The ${label(item)} blocks the window.`,
        measuredMm: mm(topMm(item.footprint)),
        limitMm: mm(opening.sillMm),
        relatedIds: [openingId],
      });
    }
  }
}

/**
 * S4: an against-wall item whose back is not to the wall.
 *
 * A sofa rotated 180 degrees satisfies every hard rule and is obviously wrong
 * to a person. This is the rule that catches it.
 */
function checkWallFacing(
  items: PlacedItem[],
  analysis: RoomAnalysis,
  push: Push,
): void {
  for (const item of items) {
    if (item.againstWallId === null) continue;
    const wall = wallById(analysis, item.againstWallId);
    if (!wall) continue;
    const inward = wallInwardNormal(wall);
    const facing = frontNormalOf(item.footprint);
    const alignment = facing[0] * inward[0] + facing[1] * inward[1];
    if (alignment < 0.3) {
      push(item.id, {
        code: "S4_BACK_NOT_TO_WALL",
        severity: "soft",
        message: `The ${label(item)} is facing the wall rather than the room.`,
        relatedIds: [wall.id],
      });
    }
  }
}

/** S5: an item out of proportion with the room it is in. */
function checkOversized(
  items: PlacedItem[],
  analysis: RoomAnalysis,
  push: Push,
): void {
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
  const roomSpan = Math.max(maxX - minX, maxZ - minZ);

  for (const item of items) {
    const widest = Math.max(item.footprint.widthMm, item.footprint.depthMm);
    if (roomSpan > 0 && widest > roomSpan * MAX_ROOM_FRACTION) {
      push(item.id, {
        code: "S5_OVERSIZED_FOR_ROOM",
        severity: "soft",
        message:
          `The ${label(item)} is ${mm(widest)} mm across in a room ` +
          `${mm(roomSpan)} mm wide.`,
        measuredMm: mm(widest),
        limitMm: mm(roomSpan * MAX_ROOM_FRACTION),
      });
      continue;
    }

    if (item.againstWallId === null) continue;
    const runs = runsFor(analysis, item.againstWallId);
    const longest =
      runs.length > 0 ? Math.max(...runs.map((run) => run.lengthMm)) : 0;
    if (
      longest > 0 &&
      item.footprint.widthMm > longest * MAX_WALL_RUN_FRACTION
    ) {
      push(item.id, {
        code: "S5_OVERSIZED_FOR_ROOM",
        severity: "soft",
        message:
          `The ${label(item)} takes up most of that wall ` +
          `(${mm(item.footprint.widthMm)} mm of ${mm(longest)} mm).`,
        measuredMm: mm(item.footprint.widthMm),
        limitMm: mm(longest * MAX_WALL_RUN_FRACTION),
      });
    }
  }
}

/** S6: the product is gone, or its price has not been checked lately. */
function checkAvailability(items: PlacedItem[], push: Push): void {
  for (const item of items) {
    if (!item.available) {
      push(item.id, {
        code: "S6_PRICE_STALE_OR_UNAVAILABLE",
        severity: "soft",
        message: `The ${label(item)} is no longer available.`,
      });
    } else if (item.priceAgeDays > STALE_PRICE_DAYS) {
      push(item.id, {
        code: "S6_PRICE_STALE_OR_UNAVAILABLE",
        severity: "soft",
        message:
          `The price for the ${label(item)} was last checked ` +
          `${item.priceAgeDays} days ago.`,
      });
    }
  }
}

/**
 * S2: the per-category clearances from `category_rules.yaml`.
 *
 * Returns the tightest gap found, which the caller reports as
 * `min_walkway_mm` and tests against S1.
 */
function checkCategoryClearances(
  items: PlacedItem[],
  push: Push,
): number | null {
  let tightest: number | null = null;
  for (let i = 0; i < items.length; i += 1) {
    for (let j = i + 1; j < items.length; j += 1) {
      const first = items[i]!;
      const second = items[j]!;
      if (isFloorCovering(first) || isFloorCovering(second)) continue;
      if (!heightOverlaps(first.footprint, second.footprint)) continue;

      const gap = gapBetween(first.footprint, second.footprint);
      if (gap <= 0) continue;
      tightest = tightest === null ? gap : Math.min(tightest, gap);

      const required = Math.max(
        ruleFor(first.category).frontClearanceMm,
        ruleFor(second.category).frontClearanceMm,
      );
      if (required > 0 && gap < required) {
        push(first.id, {
          code: "S2_CATEGORY_CLEARANCE",
          severity: "soft",
          message:
            `Only ${mm(gap)} mm between the ${label(first)} and the ` +
            `${label(second)}; ${mm(required)} mm is the usual minimum.`,
          measuredMm: mm(gap),
          limitMm: mm(required),
          relatedIds: [second.id],
        });
      }
    }
  }
  return tightest;
}

/** H6: every door reachable, and most of the free floor connected. */
function checkCirculation(
  items: PlacedItem[],
  analysis: RoomAnalysis,
): { connectedPct: number; blockedDoors: string[] } {
  const blocking = [
    ...items
      .filter((item) => !isFloorCovering(item))
      .map((item) => item.footprint),
    ...analysis.obstacles.map((obstacle) => obstacle.footprint),
  ];
  const grid = circulationGrid(blocking, analysis.baseGrid);
  const total = walkableCells(grid);
  if (total === 0) return { connectedPct: 0, blockedDoors: [] };

  const seeds: Array<[string, [number, number]]> = [];
  for (const opening of analysis.openings) {
    if (opening.type !== "door" && opening.type !== "passage") continue;
    const wall = wallById(analysis, opening.wallId);
    if (!wall) continue;
    const [x, z] = doorStandingPoint(wall, opening);
    seeds.push([opening.id, cellOf(grid, x, z)]);
  }

  if (seeds.length === 0) {
    // No door to start from. Use the largest reachable region, which is what
    // the Python side measures in the same situation.
    return {
      connectedPct: Math.trunc((100 * largestRegion(grid)) / total),
      blockedDoors: [],
    };
  }

  const first = seeds[0]!;
  const reachable = connectedComponent(grid, first[1]);
  const blocked: string[] = [];
  for (const [openingId, cell] of seeds.slice(1)) {
    if (!cellIn(reachable, cell)) blocked.push(openingId);
  }
  if (!cellIn(grid.walkable, first[1])) blocked.unshift(first[0]);

  let reached = 0;
  for (const row of reachable) for (const cell of row) if (cell) reached += 1;
  return {
    connectedPct: Math.trunc((100 * reached) / total),
    blockedDoors: blocked,
  };
}

function cellIn(mask: boolean[][], cell: [number, number]): boolean {
  const [row, column] = cell;
  if (row < 0 || row >= mask.length) return false;
  const line = mask[row]!;
  return column >= 0 && column < line.length && line[column] === true;
}

function largestRegion(
  grid: { walkable: boolean[][] } & Parameters<typeof connectedComponent>[0],
): number {
  const seen = grid.walkable.map((row) => row.map(() => false));
  let largest = 0;
  for (let row = 0; row < grid.walkable.length; row += 1) {
    const line = grid.walkable[row]!;
    for (let column = 0; column < line.length; column += 1) {
      if (!line[column] || seen[row]![column]) continue;
      const region = connectedComponent(grid, [row, column]);
      let size = 0;
      for (let r = 0; r < region.length; r += 1) {
        for (let c = 0; c < region[r]!.length; c += 1) {
          if (region[r]![c]) {
            seen[r]![c] = true;
            size += 1;
          }
        }
      }
      largest = Math.max(largest, size);
    }
  }
  return largest;
}

/**
 * Serialise to the `ValidationReport` schema.
 *
 * Key order and item order match the Python implementation exactly: the
 * parity suite compares JSON, so ordering is part of the contract rather
 * than an implementation detail.
 */
export function reportToJson(
  report: ValidationReport,
): Record<string, unknown> {
  const measured: Record<string, unknown> = {};
  if (report.minWalkwayMm !== null)
    measured["min_walkway_mm"] = report.minWalkwayMm;
  if (report.freeFloorPct !== null)
    measured["free_floor_pct"] = report.freeFloorPct;
  measured["total_price"] = {
    amount_cents: report.totalPriceCents,
    currency: "USD",
  };

  const items = [...report.items.entries()]
    .filter(([, violations]) => violations.length > 0)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([itemId, violations]) => ({
      item_id: itemId,
      violations: violations.map(violationToJson),
    }));

  return {
    schema_version: SCHEMA_VERSION,
    fits: report.fits,
    violations: report.violations.map(violationToJson),
    items,
    measured,
  };
}

function violationToJson(violation: Violation): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    code: violation.code,
    severity: violation.severity,
    message: violation.message,
  };
  if (violation.measuredMm !== undefined)
    payload["measured_mm"] = violation.measuredMm;
  if (violation.limitMm !== undefined) payload["limit_mm"] = violation.limitMm;
  if (violation.relatedIds && violation.relatedIds.length > 0) {
    payload["related_ids"] = violation.relatedIds;
  }
  return payload;
}

export type { CategoryRule };
