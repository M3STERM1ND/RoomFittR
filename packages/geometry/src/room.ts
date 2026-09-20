/**
 * L1 room analysis (implementation-plan.md 5.2).
 *
 * The TypeScript twin of `workers/layout/roomfittr_layout/room.py`. It exists
 * because the editor validates every change in the browser (5.6, and 7.1
 * lists layout-edit validation as synchronous), and the answer it gives must
 * be the one the server will give when the edit is saved. If the two drift,
 * the editor lets a user build something the server then rejects.
 *
 * The circulation grid is the part that most easily drifts, so both sides
 * define it the same way: a cell is walkable when its centre is inside the
 * floor, at least a person's radius from any wall, and at least a person's
 * radius from every item. Those are distances, not buffered polygons --
 * a buffer is an approximation whose segment count is an implementation
 * detail, and two implementations would approximate it differently.
 */

import {
  type Footprint,
  type Point2,
  type Polygon,
  containsPoint,
  corners as cornersOf,
  distanceToBoundary,
  distanceToFootprint,
  rectangleFromWall,
} from "./geometry.ts";

/** 5.2: door keep-out is `width x width` into the room, plus a 900 mm approach. */
export const DOOR_APPROACH_MM = 900;
/** 5.2: the zone in front of a window that tall items should not occupy. */
export const WINDOW_ZONE_DEPTH_MM = 600;
/** 5.2 / H6: 50 mm cells, dilated by a 380 mm person radius (a 760 mm walkway). */
export const GRID_MM = 50;
export const PERSON_RADIUS_MM = 380;
/** H6: the share of free floor that must stay connected. */
export const MIN_CONNECTED_FRACTION = 0.6;
/** Nothing in the V1 vocabulary fits a shorter wall run than this. */
export const MIN_USABLE_RUN_MM = 400;

export interface Wall {
  readonly id: string;
  readonly start: Point2;
  readonly end: Point2;
  readonly kind: "solid" | "open";
}

export interface Opening {
  readonly id: string;
  readonly wallId: string;
  readonly type: "door" | "window" | "passage";
  readonly offsetMm: number;
  readonly widthMm: number;
  readonly sillMm: number;
  readonly heightMm: number;
  readonly swing: "left" | "right" | "unknown";
}

export interface Obstacle {
  readonly id: string;
  readonly label: string;
  readonly footprint: Footprint;
}

export interface FreeRun {
  readonly wallId: string;
  readonly startMm: number;
  readonly lengthMm: number;
}

export interface StaticGrid {
  readonly walkable: boolean[][];
  readonly gridX: number[][];
  readonly gridZ: number[][];
  readonly originXMm: number;
  readonly originZMm: number;
  readonly cellMm: number;
}

export interface CirculationGrid {
  readonly walkable: boolean[][];
  readonly originXMm: number;
  readonly originZMm: number;
  readonly cellMm: number;
}

export interface RoomAnalysis {
  readonly floor: Polygon;
  readonly walls: Wall[];
  readonly openings: Opening[];
  readonly obstacles: Obstacle[];
  readonly ceilingHeightMm: number;
  readonly doorKeepouts: Map<string, Footprint>;
  readonly windowZones: Map<string, Footprint>;
  readonly freeRuns: FreeRun[];
  readonly baseGrid: StaticGrid;
}

export function wallLength(wall: Wall): number {
  return Math.hypot(wall.end[0] - wall.start[0], wall.end[1] - wall.start[1]);
}

export function wallDirection(wall: Wall): Point2 {
  const length = wallLength(wall);
  if (length < 1e-9) return [0, 0];
  return [
    (wall.end[0] - wall.start[0]) / length,
    (wall.end[1] - wall.start[1]) / length,
  ];
}

/** Left normal, which points into the room for a CCW floor polygon. */
export function wallInwardNormal(wall: Wall): Point2 {
  const [dx, dz] = wallDirection(wall);
  return [-dz, dx];
}

export function wallPointAt(wall: Wall, offsetMm: number): Point2 {
  const [dx, dz] = wallDirection(wall);
  return [wall.start[0] + dx * offsetMm, wall.start[1] + dz * offsetMm];
}

/**
 * 5.2: a `width x width` swing area plus a 900 mm approach zone.
 *
 * When the swing is unknown -- which V1 almost always leaves it (3.6) -- the
 * keep-out covers a door's width either side rather than guessing a hinge.
 * Guessing wrong puts a bookcase where the door opens, and the user finds out
 * when the door hits it.
 */
export function doorKeepout(wall: Wall, opening: Opening): Footprint {
  const swingDepth = Math.max(opening.widthMm, DOOR_APPROACH_MM);
  if (opening.swing === "left" || opening.swing === "right") {
    return rectangleFromWall(
      wall.start,
      wall.end,
      opening.offsetMm,
      opening.widthMm,
      swingDepth,
    );
  }
  const start = Math.max(opening.offsetMm - opening.widthMm, 0);
  const end = Math.min(
    opening.offsetMm + 2 * opening.widthMm,
    wallLength(wall),
  );
  return rectangleFromWall(
    wall.start,
    wall.end,
    start,
    Math.max(end - start, 1),
    swingDepth,
  );
}

/** 5.2: the 600 mm-deep strip in front of a window. A warning zone, not a keep-out. */
export function windowZone(wall: Wall, opening: Opening): Footprint {
  return rectangleFromWall(
    wall.start,
    wall.end,
    opening.offsetMm,
    opening.widthMm,
    WINDOW_ZONE_DEPTH_MM,
  );
}

/**
 * The stretches of a wall with nothing in front of them.
 *
 * `open` walls return nothing: 3.6 says the solver treats them as a boundary
 * but never places against them, and a sofa backed onto thin air is exactly
 * the result that would embarrass the product.
 */
export function freeRuns(
  wall: Wall,
  openings: Opening[],
  obstacles: Obstacle[],
): FreeRun[] {
  const length = wallLength(wall);
  if (wall.kind !== "solid" || length < MIN_USABLE_RUN_MM) return [];

  const blocked: Array<[number, number]> = [];
  for (const opening of openings) {
    if (opening.wallId !== wall.id) continue;
    // A high window leaves the wall below it usable: a sideboard can sit
    // under it. Only floor-length openings interrupt the run.
    if (opening.type === "window" && opening.sillMm >= 700) continue;
    blocked.push([opening.offsetMm, opening.offsetMm + opening.widthMm]);
  }

  const [dx, dz] = wallDirection(wall);
  for (const obstacle of obstacles) {
    const ring = cornersOf(obstacle.footprint);
    const projections = ring.map(
      ([x, z]) => (x - wall.start[0]) * dx + (z - wall.start[1]) * dz,
    );
    const distances = ring.map(([x, z]) =>
      Math.abs((x - wall.start[0]) * -dz + (z - wall.start[1]) * dx),
    );
    if (Math.min(...distances) > 300) continue;
    const lo = Math.max(Math.min(...projections), 0);
    const hi = Math.min(Math.max(...projections), length);
    if (hi > lo) blocked.push([lo, hi]);
  }

  blocked.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const runs: FreeRun[] = [];
  let cursor = 0;
  for (const [lo, hi] of blocked) {
    if (lo - cursor >= MIN_USABLE_RUN_MM) {
      runs.push({ wallId: wall.id, startMm: cursor, lengthMm: lo - cursor });
    }
    cursor = Math.max(cursor, hi);
  }
  if (length - cursor >= MIN_USABLE_RUN_MM) {
    runs.push({ wallId: wall.id, startMm: cursor, lengthMm: length - cursor });
  }
  return runs;
}

/** Everything about the room's walkability that furniture cannot change. */
export function staticGrid(floor: Polygon, cellMm = GRID_MM): StaticGrid {
  let minX = Infinity;
  let minZ = Infinity;
  let maxX = -Infinity;
  let maxZ = -Infinity;
  for (const [x, z] of floor) {
    minX = Math.min(minX, x);
    minZ = Math.min(minZ, z);
    maxX = Math.max(maxX, x);
    maxZ = Math.max(maxZ, z);
  }
  const columns = Math.max(Math.ceil((maxX - minX) / cellMm), 1);
  const rows = Math.max(Math.ceil((maxZ - minZ) / cellMm), 1);

  const walkable: boolean[][] = [];
  const gridX: number[][] = [];
  const gridZ: number[][] = [];
  for (let row = 0; row < rows; row += 1) {
    const walkRow: boolean[] = [];
    const xRow: number[] = [];
    const zRow: number[] = [];
    const z = minZ + (row + 0.5) * cellMm;
    for (let column = 0; column < columns; column += 1) {
      const x = minX + (column + 0.5) * cellMm;
      xRow.push(x);
      zRow.push(z);
      const p: Point2 = [x, z];
      walkRow.push(
        containsPoint(floor, p) &&
          distanceToBoundary(p, floor) >= PERSON_RADIUS_MM,
      );
    }
    walkable.push(walkRow);
    gridX.push(xRow);
    gridZ.push(zRow);
  }
  return { walkable, gridX, gridZ, originXMm: minX, originZMm: minZ, cellMm };
}

/**
 * The walkable cells with `blocked` rectangles in place.
 *
 * A cell is blocked when its centre lies within a person's radius of any
 * item -- exactly the dilation a buffer would produce, computed as a distance
 * so this and the Python side mark the same cells.
 */
export function circulationGrid(
  blocked: Footprint[],
  base: StaticGrid,
): CirculationGrid {
  const walkable = base.walkable.map((row) => [...row]);
  for (const item of blocked) {
    if (item.widthMm <= 0 || item.depthMm <= 0) continue;
    for (let row = 0; row < walkable.length; row += 1) {
      const walkRow = walkable[row]!;
      const xRow = base.gridX[row]!;
      const zRow = base.gridZ[row]!;
      for (let column = 0; column < walkRow.length; column += 1) {
        if (!walkRow[column]) continue;
        if (
          distanceToFootprint([xRow[column]!, zRow[column]!], item) <
          PERSON_RADIUS_MM
        ) {
          walkRow[column] = false;
        }
      }
    }
  }
  return {
    walkable,
    originXMm: base.originXMm,
    originZMm: base.originZMm,
    cellMm: base.cellMm,
  };
}

export function cellOf(
  grid: CirculationGrid,
  xMm: number,
  zMm: number,
): [number, number] {
  return [
    Math.floor((zMm - grid.originZMm) / grid.cellMm),
    Math.floor((xMm - grid.originXMm) / grid.cellMm),
  ];
}

export function walkableCells(grid: CirculationGrid): number {
  let count = 0;
  for (const row of grid.walkable) for (const cell of row) if (cell) count += 1;
  return count;
}

/**
 * Flood fill from `seed` over walkable cells, 4-connected.
 *
 * 4-connected rather than 8-: diagonal moves would let a person slip between
 * two items touching at a corner, which they cannot do.
 */
export function connectedComponent(
  grid: CirculationGrid,
  seed: [number, number],
): boolean[][] {
  const rows = grid.walkable.length;
  const columns = rows > 0 ? grid.walkable[0]!.length : 0;
  const seen: boolean[][] = grid.walkable.map((row) => row.map(() => false));
  const [seedRow, seedColumn] = seed;
  if (seedRow < 0 || seedRow >= rows || seedColumn < 0 || seedColumn >= columns)
    return seen;
  if (!grid.walkable[seedRow]![seedColumn]) return seen;

  const queue: Array<[number, number]> = [seed];
  seen[seedRow]![seedColumn] = true;
  while (queue.length > 0) {
    const [row, column] = queue.shift()!;
    for (const [dr, dc] of [
      [1, 0],
      [-1, 0],
      [0, 1],
      [0, -1],
    ] as const) {
      const nr = row + dr;
      const nc = column + dc;
      if (nr < 0 || nr >= rows || nc < 0 || nc >= columns) continue;
      if (!grid.walkable[nr]![nc] || seen[nr]![nc]) continue;
      seen[nr]![nc] = true;
      queue.push([nr, nc]);
    }
  }
  return seen;
}

/** A point just inside the room in front of a door, where a person would stand. */
export function doorStandingPoint(wall: Wall, opening: Opening): Point2 {
  const centre = wallPointAt(wall, opening.offsetMm + opening.widthMm / 2);
  const [nx, nz] = wallInwardNormal(wall);
  const reach = PERSON_RADIUS_MM + GRID_MM;
  return [centre[0] + nx * reach, centre[1] + nz * reach];
}

/** The `RoomModel` document shape this module reads. */
export interface RoomModel {
  floor_polygon: Array<[number, number]>;
  ceiling_height_mm: number;
  walls: Array<{
    id: string;
    start: [number, number];
    end: [number, number];
    kind: "solid" | "open";
  }>;
  openings: Array<{
    id: string;
    wall_id: string;
    type: "door" | "window" | "passage";
    offset_mm: number;
    width_mm: number;
    sill_mm: number;
    height_mm: number;
    swing?: "left" | "right" | "unknown";
  }>;
  fixed_obstacles?: Array<{
    id: string;
    label: string;
    center: [number, number];
    size: [number, number, number];
    yaw_deg?: number;
  }>;
}

/** Run L1 over a `RoomModel` document. */
export function analyse(model: RoomModel): RoomAnalysis {
  const floor: Polygon = model.floor_polygon.map(([x, z]) => [x, z] as Point2);
  const walls: Wall[] = model.walls.map((raw) => ({
    id: raw.id,
    start: [raw.start[0], raw.start[1]] as Point2,
    end: [raw.end[0], raw.end[1]] as Point2,
    kind: raw.kind,
  }));
  const openings: Opening[] = model.openings.map((raw) => ({
    id: raw.id,
    wallId: raw.wall_id,
    type: raw.type,
    offsetMm: raw.offset_mm,
    widthMm: raw.width_mm,
    sillMm: raw.sill_mm,
    heightMm: raw.height_mm,
    swing: raw.swing ?? "unknown",
  }));
  const obstacles: Obstacle[] = (model.fixed_obstacles ?? []).map((raw) => ({
    id: raw.id,
    label: raw.label,
    footprint: {
      centerXMm: raw.center[0],
      centerZMm: raw.center[1],
      widthMm: raw.size[0],
      depthMm: raw.size[1],
      heightMm: raw.size[2],
      rotationDeg: raw.yaw_deg ?? 0,
      elevationMm: 0,
    },
  }));

  const byId = new Map(walls.map((wall) => [wall.id, wall]));
  const doorKeepouts = new Map<string, Footprint>();
  const windowZones = new Map<string, Footprint>();
  for (const opening of openings) {
    const wall = byId.get(opening.wallId);
    if (!wall) continue;
    if (opening.type === "door" || opening.type === "passage") {
      doorKeepouts.set(opening.id, doorKeepout(wall, opening));
    } else if (opening.type === "window") {
      windowZones.set(opening.id, windowZone(wall, opening));
    }
  }

  const runs: FreeRun[] = [];
  for (const wall of walls) runs.push(...freeRuns(wall, openings, obstacles));

  return {
    floor,
    walls,
    openings,
    obstacles,
    ceilingHeightMm: model.ceiling_height_mm,
    doorKeepouts,
    windowZones,
    freeRuns: runs,
    baseGrid: staticGrid(floor),
  };
}

export function wallById(
  analysis: RoomAnalysis,
  wallId: string,
): Wall | undefined {
  return analysis.walls.find((wall) => wall.id === wallId);
}

export function runsFor(analysis: RoomAnalysis, wallId: string): FreeRun[] {
  return analysis.freeRuns.filter((run) => run.wallId === wallId);
}
