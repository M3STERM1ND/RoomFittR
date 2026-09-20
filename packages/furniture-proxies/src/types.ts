/**
 * Parametric furniture proxies (implementation-plan.md 4.10).
 *
 * Retailers almost never publish usable 3D models, so V1 renders a catalog
 * product as a low-poly stand-in built from its width, depth and height. The
 * product photo stays the primary "what does it look like" signal; the proxy's
 * job is to be **exactly the right size** in the room.
 *
 * **A proxy is a list of oriented boxes.** Not a mesh, not a parametric
 * surface. Three reasons:
 *
 * - 4.10 budgets 2k triangles per item and 9.2 budgets 100k for the whole
 *   scene. A box is 12 triangles, so a ten-part sofa is 120 -- the budget
 *   stops being something to manage.
 * - The viewer can render every proxy of a family with one instanced draw
 *   call, which is what keeps 9.2's 150-draw-call budget reachable.
 * - It is testable. "Does the sofa fit inside its stated dimensions" is a
 *   loop over boxes, and that is the property that actually matters: the
 *   validator reserved exactly `width x depth` of floor for this thing.
 *
 * Units are millimetres, matching 2.4 and the layout engine. The viewer
 * converts to metres at the same boundary the room shell does.
 */

/** One box of a proxy, in the item's local frame. */
export interface ProxyPart {
  /** Centre in the item's local frame: x right, y up, z towards the viewer. */
  readonly center: readonly [number, number, number];
  readonly size: readonly [number, number, number];
  /** Rotation about +Y in degrees. Only the arc lamp needs anything else. */
  readonly rotationDeg?: number;
  /**
   * Which material slot this part uses.
   *
   * 4.10 tints a proxy with the product's dominant colour and a basic
   * material type. Splitting frame from surface means a wooden-legged fabric
   * sofa reads as one, rather than as a single blob of one colour.
   */
  readonly material: "body" | "frame" | "surface" | "glass";
}

export interface Proxy {
  readonly family: string;
  readonly parts: ProxyPart[];
}

/** Overall dimensions, as the catalog records them (4.6). */
export interface Dimensions {
  readonly widthMm: number;
  readonly depthMm: number;
  readonly heightMm: number;
}

/** 12 triangles per box. 4.10 budgets 2k per item. */
export const TRIANGLES_PER_PART = 12;
export const MAX_TRIANGLES_PER_ITEM = 2000;

export function triangleCount(proxy: Proxy): number {
  return proxy.parts.length * TRIANGLES_PER_PART;
}

/**
 * The axis-aligned extent a proxy actually occupies, in its local frame.
 *
 * Used by the tests to assert a proxy stays inside the footprint the
 * validator reserved for it. A proxy that overflows would collide with
 * something the validator said it cleared.
 */
export function extent(proxy: Proxy): {
  min: [number, number, number];
  max: [number, number, number];
} {
  const min: [number, number, number] = [Infinity, Infinity, Infinity];
  const max: [number, number, number] = [-Infinity, -Infinity, -Infinity];

  for (const part of proxy.parts) {
    // A rotated part's extent is its rotated corners, not its size.
    const angle = ((part.rotationDeg ?? 0) * Math.PI) / 180;
    const cos = Math.abs(Math.cos(angle));
    const sin = Math.abs(Math.sin(angle));
    const halfX = (part.size[0] * cos + part.size[2] * sin) / 2;
    const halfZ = (part.size[0] * sin + part.size[2] * cos) / 2;
    const half: [number, number, number] = [halfX, part.size[1] / 2, halfZ];

    for (let axis = 0; axis < 3; axis += 1) {
      min[axis] = Math.min(min[axis]!, part.center[axis]! - half[axis]!);
      max[axis] = Math.max(max[axis]!, part.center[axis]! + half[axis]!);
    }
  }
  return { min, max };
}
