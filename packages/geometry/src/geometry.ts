/**
 * Geometric primitives for layout (implementation-plan.md 5.5).
 *
 * The TypeScript twin of `workers/layout/roomfittr_layout/geometry.py`. The
 * two are kept deliberately line-for-line comparable, because 5.5 requires
 * them to produce identical validation reports and `fixtures/validation/`
 * pins that in CI. A clever refactor on one side that has no counterpart on
 * the other is how the two drift apart.
 *
 * **The rotation convention, stated once.** `rotationDeg` is a right-handed
 * rotation about **+Y**, matching 2.4, `three.js`'s `object.rotation.y`, and
 * the pipeline's `align.yaw_rotation`. In the XZ plane:
 *
 *     x' =  x*cos(t) + z*sin(t)
 *     z' = -x*sin(t) + z*cos(t)
 *
 * A *positive* rotation carries +X towards **-Z**, so a heading measured as
 * `atan2(z, x)` decreases. The other handedness -- the one that falls out of
 * writing the familiar 2D rotation matrix by reflex -- is wrong here, and
 * wrong in a way nothing downstream catches: the layout still validates and
 * every sofa faces 90 degrees from where it should.
 *
 * **No dependencies.** This runs in the browser on every pointer-move while
 * dragging (5.6, budget: 8 ms p95), so there is no geometry library here --
 * a polygon is an array of points and the handful of operations needed are
 * written out.
 */

/** 5.5: "polygon containment with a tolerance of 5 mm". */
export const TOLERANCE_MM = 5;

export type Point2 = readonly [number, number];
/** A simple polygon as a ring of vertices; the closing edge is implicit. */
export type Polygon = readonly Point2[];

export interface Footprint {
  readonly centerXMm: number;
  readonly centerZMm: number;
  readonly widthMm: number;
  readonly depthMm: number;
  readonly heightMm: number;
  readonly rotationDeg: number;
  /** Bottom of the item: 0 for anything standing on the floor. */
  readonly elevationMm: number;
}

export function footprint(
  partial: Partial<Footprint> &
    Pick<
      Footprint,
      "centerXMm" | "centerZMm" | "widthMm" | "depthMm" | "heightMm"
    >,
): Footprint {
  return { rotationDeg: 0, elevationMm: 0, ...partial };
}

/** Rotate an XZ point about +Y, right-handed. See the module comment. */
export function rotateXz(x: number, z: number, degrees: number): Point2 {
  const angle = (degrees * Math.PI) / 180;
  const cos = Math.cos(angle);
  const sin = Math.sin(angle);
  return [x * cos + z * sin, -x * sin + z * cos];
}

export function topMm(f: Footprint): number {
  return f.elevationMm + f.heightMm;
}

/**
 * The four XZ corners after rotation.
 *
 * Local axes before rotation: width along X, depth along Z, and the item's
 * **front is -Z** (2.4).
 */
export function corners(f: Footprint): Point2[] {
  const halfW = f.widthMm / 2;
  const halfD = f.depthMm / 2;
  const local: Point2[] = [
    [-halfW, -halfD],
    [halfW, -halfD],
    [halfW, halfD],
    [-halfW, halfD],
  ];
  return local.map(([x, z]) => {
    const [rx, rz] = rotateXz(x, z, f.rotationDeg);
    return [f.centerXMm + rx, f.centerZMm + rz] as Point2;
  });
}

/**
 * The unit direction the item faces: local -Z, rotated.
 *
 * Used by S4 and by the editor's snapping. A sign error here faces every
 * sofa at the wall, which is a valid layout and an absurd one.
 */
export function frontNormal(f: Footprint): Point2 {
  return rotateXz(0, -1, f.rotationDeg);
}

/**
 * Whether two height intervals genuinely intersect.
 *
 * Tolerant on purpose: a rug 10 mm tall and a sofa whose legs start at
 * exactly 10 mm are stacked, not colliding.
 */
export function heightOverlaps(a: Footprint, b: Footprint): boolean {
  const shared =
    Math.min(topMm(a), topMm(b)) - Math.max(a.elevationMm, b.elevationMm);
  return shared > TOLERANCE_MM;
}

/**
 * Overlap depth between two oriented rectangles in mm; 0 means separate.
 *
 * The separating axis theorem: two convex shapes are disjoint exactly when
 * some axis exists on which their projections do not overlap. For rectangles
 * the only candidate axes are the edge normals, so checking four is
 * exhaustive rather than approximate.
 *
 * Returning the *depth* rather than a boolean is what lets the UI say "the
 * sofa overlaps the table by 40 mm", which 5.5 requires of every violation.
 */
export function separatingAxisOverlap(a: Footprint, b: Footprint): number {
  const cornersA = corners(a);
  const cornersB = corners(b);
  let smallest = Number.POSITIVE_INFINITY;

  for (const ring of [cornersA, cornersB]) {
    for (let index = 0; index < 2; index += 1) {
      const from = ring[index]!;
      const to = ring[(index + 1) % 4]!;
      const edge: Point2 = [to[0] - from[0], to[1] - from[1]];
      const axis: Point2 = [-edge[1], edge[0]];
      const norm = Math.hypot(axis[0], axis[1]);
      if (norm < 1e-9) continue;
      const unit: Point2 = [axis[0] / norm, axis[1] / norm];

      const projectedA = cornersA.map((p) => p[0] * unit[0] + p[1] * unit[1]);
      const projectedB = cornersB.map((p) => p[0] * unit[0] + p[1] * unit[1]);
      const overlap =
        Math.min(Math.max(...projectedA), Math.max(...projectedB)) -
        Math.max(Math.min(...projectedA), Math.min(...projectedB));
      if (overlap <= 0) return 0;
      smallest = Math.min(smallest, overlap);
    }
  }
  return Number.isFinite(smallest) ? smallest : 0;
}

function distancePointToSegment(p: Point2, a: Point2, b: Point2): number {
  const vx = b[0] - a[0];
  const vz = b[1] - a[1];
  const lengthSquared = vx * vx + vz * vz;
  if (lengthSquared < 1e-12) return Math.hypot(p[0] - a[0], p[1] - a[1]);
  const t = Math.max(
    0,
    Math.min(1, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vz) / lengthSquared),
  );
  return Math.hypot(p[0] - (a[0] + t * vx), p[1] - (a[1] + t * vz));
}

/**
 * Distance from an XZ point to a polygon's boundary.
 *
 * Exported because the circulation grid uses it directly: wall clearance is
 * defined as this distance rather than as `floor.buffer(-radius)` so that the
 * Python side, which has a geometry library and could take the shortcut,
 * marks exactly the same cells (5.5 parity).
 */
export function distanceToBoundary(p: Point2, polygon: Polygon): number {
  let best = Number.POSITIVE_INFINITY;
  for (let index = 0; index < polygon.length; index += 1) {
    const a = polygon[index]!;
    const b = polygon[(index + 1) % polygon.length]!;
    best = Math.min(best, distancePointToSegment(p, a, b));
  }
  return best;
}

/**
 * Point-in-polygon by ray casting, with points exactly on the boundary
 * counted as inside.
 *
 * The boundary case is not pedantry: the solver puts items flush against
 * walls, so their corners land on the polygon's edge by construction, and a
 * strict test would report every one of them as outside.
 */
export function containsPoint(polygon: Polygon, p: Point2): boolean {
  if (distanceToBoundary(p, polygon) <= 1e-9) return true;

  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i, i += 1) {
    const a = polygon[i]!;
    const b = polygon[j]!;
    const straddles = a[1] > p[1] !== b[1] > p[1];
    if (!straddles) continue;
    const crossingX = ((b[0] - a[0]) * (p[1] - a[1])) / (b[1] - a[1]) + a[0];
    if (p[0] < crossingX) inside = !inside;
  }
  return inside;
}

/**
 * How far the footprint sticks out of the room, in mm; 0 if inside.
 *
 * Measured from the worst corner, because a corner is a point and a point's
 * distance to the boundary it escaped is exactly the number the user should
 * read ("the bookshelf is 40 mm into the wall").
 */
export function protrusion(f: Footprint, room: Polygon): number {
  let worst = 0;
  for (const corner of corners(f)) {
    if (!containsPoint(room, corner)) {
      worst = Math.max(worst, distanceToBoundary(corner, room));
    }
  }
  return worst;
}

/**
 * Shortest distance between two footprints; 0 if they touch or overlap.
 *
 * Edge-to-edge rather than a separating-axis projection: SAT's per-axis
 * separations understate a diagonal gap, and the number here is shown to the
 * user as a walkway width.
 */
export function gapBetween(a: Footprint, b: Footprint): number {
  if (separatingAxisOverlap(a, b) > 0) return 0;
  const ringA = corners(a);
  const ringB = corners(b);

  let best = Number.POSITIVE_INFINITY;
  for (const p of ringA) best = Math.min(best, distanceToBoundary(p, ringB));
  for (const p of ringB) best = Math.min(best, distanceToBoundary(p, ringA));
  return best;
}

/** Area of a simple polygon, by the shoelace formula. */
export function polygonArea(polygon: Polygon): number {
  let twice = 0;
  for (let i = 0; i < polygon.length; i += 1) {
    const a = polygon[i]!;
    const b = polygon[(i + 1) % polygon.length]!;
    twice += a[0] * b[1] - b[0] * a[1];
  }
  return Math.abs(twice) / 2;
}

export interface Bounds {
  readonly minX: number;
  readonly minZ: number;
  readonly maxX: number;
  readonly maxZ: number;
}

export function bounds(polygon: Polygon): Bounds {
  const xs = polygon.map((p) => p[0]);
  const zs = polygon.map((p) => p[1]);
  return {
    minX: Math.min(...xs),
    minZ: Math.min(...zs),
    maxX: Math.max(...xs),
    maxZ: Math.max(...zs),
  };
}

/**
 * Distance from an XZ point to an oriented rectangle; 0 if inside.
 *
 * Computed in the rectangle's own frame, where the problem is the
 * axis-aligned one. The clamped form handles the corner case as a real 2D
 * distance rather than as the larger of the two axis distances.
 *
 * The circulation grid asks this for every cell, and the Python side -- which
 * has a geometry library and could have called `buffer()` -- computes the
 * same distance instead, so both mark the same cells (5.5 parity).
 */
export function distanceToFootprint(p: Point2, f: Footprint): number {
  const [localX, localZ] = rotateXz(
    p[0] - f.centerXMm,
    p[1] - f.centerZMm,
    -f.rotationDeg,
  );
  const outsideX = Math.max(Math.abs(localX) - f.widthMm / 2, 0);
  const outsideZ = Math.max(Math.abs(localZ) - f.depthMm / 2, 0);
  return Math.hypot(outsideX, outsideZ);
}

/**
 * A rectangle sitting against a wall, extending `depthMm` into the room.
 *
 * Used for door keep-outs and window zones (5.2). "Into the room" is the
 * wall's left normal, which for the counter-clockwise floor polygon the
 * schema requires points inwards.
 */
export function rectangleFromWall(
  start: Point2,
  end: Point2,
  offsetMm: number,
  widthMm: number,
  depthMm: number,
  heightMm = 1,
): Footprint {
  const dx = end[0] - start[0];
  const dz = end[1] - start[1];
  const length = Math.hypot(dx, dz);
  if (length < 1e-9) {
    return footprint({
      centerXMm: 0,
      centerZMm: 0,
      widthMm: 0,
      depthMm: 0,
      heightMm: 0,
    });
  }
  const ux = dx / length;
  const uz = dz / length;
  const nx = -uz;
  const nz = ux;

  const along = offsetMm + widthMm / 2;
  const out = depthMm / 2;
  return {
    centerXMm: start[0] + ux * along + nx * out,
    centerZMm: start[1] + uz * along + nz * out,
    widthMm,
    depthMm,
    heightMm,
    // The rectangle's local +X runs along the wall. A +Y yaw of theta puts
    // local +X at heading -theta, hence the negation.
    rotationDeg: modulo(-((Math.atan2(uz, ux) * 180) / Math.PI), 360),
    elevationMm: 0,
  };
}

/** Python's `%`, which returns a non-negative result for a negative left operand. */
export function modulo(value: number, divisor: number): number {
  return ((value % divisor) + divisor) % divisor;
}
