/**
 * The proxy families (implementation-plan.md 4.10).
 *
 * One generator per family, each building its boxes from the product's own
 * width, depth and height. The proportions inside -- arm width, leg height,
 * seat height, headboard thickness -- are fixed fractions chosen so the shape
 * reads correctly at any size a real product comes in.
 *
 * **Everything is built inside the stated dimensions.** The layout engine
 * reserved exactly `width x depth` of floor and cleared exactly `height` of
 * air; a proxy that overflows collides with something the validator certified
 * as clear, and the user sees furniture inside a wall in a layout the app
 * called valid.
 *
 * **Every size is derived by splitting what is left**, never by subtracting
 * and hoping. The absolute bands below are written for real furniture -- a
 * table top is 25-70 mm thick -- and a catalog row with a wrong dimension
 * breaks that immediately: a "table" 12 mm tall wants a 25 mm top, leaving
 * its legs a height of -13 mm. 9.3 rates wrong catalog dimensions R7, "High"
 * likelihood, so that is expected input rather than a hypothetical, and the
 * proxy has to degrade into something odd-looking rather than into inverted
 * geometry.
 *
 * The local frame is x right, y up, z towards the viewer, with the origin at
 * the centre of the footprint and y = 0 at the floor. The item's **front is
 * -Z** (2.4), so a sofa's back sits at +Z.
 */

import type { Dimensions, Proxy, ProxyPart } from "./types.ts";

type Generator = (dimensions: Dimensions) => ProxyPart[];

/**
 * A proportion of `total`, clamped to an absolute band, and never more than
 * most of what there is. See the module comment for why the last clause
 * matters.
 */
function share(
  total: number,
  fraction: number,
  min: number,
  max: number,
): number {
  if (!(total > 0)) return 1;
  const wanted = Math.max(min, Math.min(max, total * fraction));
  return Math.max(Math.min(wanted, total * 0.9), total * 0.02);
}

/** What is left of `total` after `taken`, never zero or negative. */
function rest(total: number, taken: number): number {
  return Math.max(total - taken, total * 0.02);
}

function box(
  center: readonly [number, number, number],
  size: readonly [number, number, number],
  material: ProxyPart["material"] = "body",
): ProxyPart {
  return { center, size, material };
}

/** Four legs inset from the corners, carrying a body above them. */
function legs(
  widthMm: number,
  depthMm: number,
  legHeight: number,
  thickness: number,
): ProxyPart[] {
  const safe = Math.min(thickness, widthMm * 0.3, depthMm * 0.3);
  const inset = safe / 2 + Math.min(widthMm, depthMm) * 0.04;
  const x = Math.max(widthMm / 2 - inset, 0);
  const z = Math.max(depthMm / 2 - inset, 0);
  return [
    [x, z],
    [-x, z],
    [x, -z],
    [-x, -z],
  ].map(([cx, cz]) =>
    box([cx!, legHeight / 2, cz!], [safe, legHeight, safe], "frame"),
  );
}

const seating =
  (armFraction: number): Generator =>
  ({ widthMm, depthMm, heightMm }) => {
    const armWidth = Math.min(
      share(widthMm, armFraction, 90, 260),
      widthMm * 0.25,
    );
    const backDepth = Math.min(share(depthMm, 0.16, 90, 220), depthMm * 0.4);
    const legHeight = share(heightMm, 0.14, 60, 180);
    const above = rest(heightMm, legHeight);
    const seatHeight = share(above, 0.45, 100, 400);
    const seatWidth = rest(widthMm, 2 * armWidth);
    const seatDepth = rest(depthMm, backDepth);

    return [
      ...legs(widthMm, depthMm, legHeight, Math.min(70, armWidth * 0.6)),
      // Seat, set forward of the back cushion.
      box(
        [0, legHeight + seatHeight / 2, -backDepth / 2],
        [seatWidth, seatHeight, seatDepth],
      ),
      // Back, full remaining height, against +Z.
      box(
        [0, legHeight + above / 2, depthMm / 2 - backDepth / 2],
        [widthMm, above, backDepth],
      ),
      // Arms.
      box(
        [widthMm / 2 - armWidth / 2, legHeight + above * 0.42, -backDepth / 2],
        [armWidth, above * 0.84, seatDepth],
      ),
      box(
        [
          -(widthMm / 2 - armWidth / 2),
          legHeight + above * 0.42,
          -backDepth / 2,
        ],
        [armWidth, above * 0.84, seatDepth],
      ),
    ];
  };

/**
 * An L-shaped sectional: a seating block plus a chaise along one side.
 *
 * The chaise lives inside the stated footprint rather than being bolted on
 * beside it, so the thing still occupies exactly the floor the solver
 * reserved.
 */
const sectional: Generator = ({ widthMm, depthMm, heightMm }) => {
  const chaiseWidth = Math.min(share(widthMm, 0.36, 500, 1200), widthMm * 0.45);
  const mainWidth = rest(widthMm, chaiseWidth);
  const mainDepth = depthMm * 0.62;

  const main = seating(0.1)({
    widthMm: mainWidth,
    depthMm: mainDepth,
    heightMm,
  }).map((part) => ({
    ...part,
    center: [
      part.center[0] - chaiseWidth / 2,
      part.center[1],
      part.center[2] + (depthMm - mainDepth) / 2,
    ] as const,
  }));

  const seatHeight = share(heightMm, 0.46, 340, 500);
  return [
    ...main,
    box(
      [widthMm / 2 - chaiseWidth / 2, seatHeight / 2, 0],
      [chaiseWidth, seatHeight, depthMm],
    ),
  ];
};

const tableRect: Generator = ({ widthMm, depthMm, heightMm }) => {
  const topThickness = share(heightMm, 0.12, 25, 70);
  const legHeight = rest(heightMm, topThickness);
  const legThickness = share(Math.min(widthMm, depthMm), 0.08, 40, 110);
  return [
    ...legs(widthMm, depthMm, legHeight, legThickness),
    box(
      [0, heightMm - topThickness / 2, 0],
      [widthMm, topThickness, depthMm],
      "surface",
    ),
  ];
};

/**
 * A round table, approximated by a square top on a central column.
 *
 * Not a cylinder: a cylinder is dozens of triangles for a shape the user
 * reads from the product photo anyway, and 4.10 is explicit that the proxy
 * exists to be the right *size* rather than the right shape.
 */
const tableRound: Generator = ({ widthMm, depthMm, heightMm }) => {
  const diameter = Math.min(widthMm, depthMm);
  const topThickness = share(heightMm, 0.12, 25, 70);
  const columnHeight = rest(heightMm, topThickness);
  const columnWidth = share(diameter, 0.16, 70, 220);
  const footHeight = Math.min(60, heightMm * 0.2);
  return [
    box(
      [0, columnHeight / 2, 0],
      [columnWidth, columnHeight, columnWidth],
      "frame",
    ),
    box(
      [0, footHeight / 2, 0],
      [diameter * 0.55, footHeight, diameter * 0.55],
      "frame",
    ),
    box(
      [0, heightMm - topThickness / 2, 0],
      [diameter, topThickness, diameter],
      "surface",
    ),
  ];
};

/** A carcass with a back panel and evenly spaced shelves. */
const shelving =
  (closed: boolean): Generator =>
  ({ widthMm, depthMm, heightMm }) => {
    const panel = Math.min(
      share(Math.min(widthMm, depthMm), 0.06, 18, 40),
      heightMm * 0.2,
      widthMm * 0.2,
      depthMm * 0.4,
    );
    const shelfCount = Math.max(2, Math.min(6, Math.round(heightMm / 400)));
    const parts: ProxyPart[] = [
      box(
        [-(widthMm - panel) / 2, heightMm / 2, 0],
        [panel, heightMm, depthMm],
        "frame",
      ),
      box(
        [(widthMm - panel) / 2, heightMm / 2, 0],
        [panel, heightMm, depthMm],
        "frame",
      ),
      box(
        [0, heightMm / 2, (depthMm - panel) / 2],
        [widthMm, heightMm, panel],
        "body",
      ),
      box([0, panel / 2, 0], [widthMm, panel, depthMm], "frame"),
      box([0, heightMm - panel / 2, 0], [widthMm, panel, depthMm], "frame"),
    ];
    for (let index = 1; index < shelfCount; index += 1) {
      parts.push(
        box(
          [0, (heightMm / shelfCount) * index, 0],
          [rest(widthMm, 2 * panel), panel, rest(depthMm, panel)],
          "frame",
        ),
      );
    }
    if (closed) {
      // Doors on the front face, split down the middle.
      const doorWidth = rest(widthMm, 2 * panel) / 2;
      for (const side of [-1, 1]) {
        parts.push(
          box(
            [(side * doorWidth) / 2, heightMm / 2, -(depthMm - panel) / 2],
            [doorWidth, rest(heightMm, 2 * panel), panel],
            "surface",
          ),
        );
      }
    }
    return parts;
  };

/** A chest of drawers: a carcass with drawer fronts. */
const drawers: Generator = ({ widthMm, depthMm, heightMm }) => {
  const legHeight = share(heightMm, 0.08, 40, 140);
  const bodyHeight = rest(heightMm, legHeight);
  const drawerCount = Math.max(2, Math.min(5, Math.round(bodyHeight / 260)));
  const panel = Math.min(
    share(Math.min(widthMm, depthMm), 0.06, 18, 40),
    (bodyHeight * 0.4) / (drawerCount + 1),
    widthMm * 0.2,
    depthMm * 0.4,
  );
  const drawerHeight =
    rest(bodyHeight, panel * (drawerCount + 1)) / drawerCount;

  const parts: ProxyPart[] = [
    ...legs(widthMm, depthMm, legHeight, Math.min(70, panel * 2)),
    box(
      [0, legHeight + bodyHeight / 2, 0],
      [widthMm, bodyHeight, depthMm],
      "body",
    ),
  ];
  for (let index = 0; index < drawerCount; index += 1) {
    const y =
      legHeight + panel + drawerHeight / 2 + index * (drawerHeight + panel);
    parts.push(
      box(
        [0, y, -(depthMm - panel) / 2],
        [rest(widthMm, 2 * panel), drawerHeight, panel],
        "surface",
      ),
    );
  }
  return parts;
};

/** A platform bed: mattress on a low base, with an optional headboard. */
const bed =
  (withHeadboard: boolean): Generator =>
  ({ widthMm, depthMm, heightMm }) => {
    const headboardDepth = withHeadboard
      ? Math.min(share(depthMm, 0.05, 50, 120), depthMm * 0.15)
      : 0;
    // With a headboard the mattress only occupies the lower part of the
    // stated height; without one, the height *is* the mattress top.
    const sleepingHeight = withHeadboard ? heightMm * 0.62 : heightMm;
    const baseHeight = share(sleepingHeight, 0.35, 120, 300);
    const mattressHeight = share(
      rest(sleepingHeight, baseHeight),
      0.8,
      120,
      320,
    );
    const mattressDepth = rest(depthMm, headboardDepth);

    const parts: ProxyPart[] = [
      box(
        [0, baseHeight / 2, -headboardDepth / 2],
        [widthMm, baseHeight, mattressDepth],
        "frame",
      ),
      box(
        [0, baseHeight + mattressHeight / 2, -headboardDepth / 2],
        [widthMm * 0.97, mattressHeight, mattressDepth * 0.97],
        "body",
      ),
    ];
    if (withHeadboard) {
      parts.push(
        box(
          [0, heightMm / 2, depthMm / 2 - headboardDepth / 2],
          [widthMm, heightMm, headboardDepth],
          "surface",
        ),
      );
    }
    return parts;
  };

/** A rug: one thin slab. `heightMm` is its pile depth (4.5 defaults it to 10). */
const rug: Generator = ({ widthMm, depthMm, heightMm }) => [
  box([0, heightMm / 2, 0], [widthMm, heightMm, depthMm], "surface"),
];

const lampArc: Generator = ({ widthMm, depthMm, heightMm }) => {
  const baseDiameter = Math.min(widthMm, depthMm) * 0.9;
  const poleWidth = share(baseDiameter, 0.12, 25, 60);
  const shadeWidth = Math.min(widthMm, depthMm) * 0.8;
  const shadeHeight = share(heightMm, 0.14, 150, 320);
  const poleHeight = rest(heightMm, shadeHeight);
  const baseHeight = Math.min(40, heightMm * 0.15);
  return [
    box(
      [0, baseHeight / 2, 0],
      [baseDiameter, baseHeight, baseDiameter],
      "frame",
    ),
    box([0, poleHeight / 2, 0], [poleWidth, poleHeight, poleWidth], "frame"),
    box(
      [0, heightMm - shadeHeight / 2, 0],
      [shadeWidth, shadeHeight, shadeWidth],
      "surface",
    ),
  ];
};

const chairUpright: Generator = ({ widthMm, depthMm, heightMm }) => {
  const seatHeight = Math.min(share(heightMm, 0.48, 380, 500), heightMm * 0.7);
  const seatThickness = share(seatHeight, 0.12, 35, 80);
  const legThickness = share(Math.min(widthMm, depthMm), 0.1, 30, 70);
  const backThickness = Math.min(share(depthMm, 0.12, 40, 90), depthMm * 0.4);
  const backHeight = rest(heightMm, seatHeight);
  return [
    ...legs(widthMm, depthMm, rest(seatHeight, seatThickness), legThickness),
    box(
      [0, seatHeight - seatThickness / 2, 0],
      [widthMm, seatThickness, depthMm],
      "surface",
    ),
    box(
      [0, seatHeight + backHeight / 2, depthMm / 2 - backThickness / 2],
      [widthMm, backHeight, backThickness],
      "body",
    ),
  ];
};

const officeChair: Generator = ({ widthMm, depthMm, heightMm }) => {
  const seatHeight = Math.min(share(heightMm, 0.45, 380, 520), heightMm * 0.7);
  const seatThickness = share(seatHeight, 0.14, 45, 100);
  const baseDiameter = Math.min(widthMm, depthMm) * 0.95;
  const baseHeight = Math.min(60, heightMm * 0.12);
  const backThickness = Math.min(share(depthMm, 0.12, 40, 90), depthMm * 0.4);
  const backHeight = rest(heightMm, seatHeight);
  const columnWidth = Math.min(90, widthMm * 0.3, depthMm * 0.3);
  return [
    box(
      [0, baseHeight / 2, 0],
      [baseDiameter, baseHeight, baseDiameter],
      "frame",
    ),
    box(
      [0, seatHeight / 2, 0],
      [columnWidth, seatHeight, columnWidth],
      "frame",
    ),
    box(
      [0, seatHeight - seatThickness / 2, 0],
      [widthMm, seatThickness, depthMm],
      "body",
    ),
    box(
      [0, seatHeight + backHeight / 2, depthMm / 2 - backThickness / 2],
      [widthMm * 0.85, backHeight, backThickness],
      "body",
    ),
  ];
};

/** A low upholstered block: ottomans and benches. */
const block: Generator = ({ widthMm, depthMm, heightMm }) => {
  const legHeight = share(heightMm, 0.22, 50, 160);
  const bodyHeight = rest(heightMm, legHeight);
  return [
    ...legs(widthMm, depthMm, legHeight, Math.min(60, widthMm * 0.08)),
    box([0, legHeight + bodyHeight / 2, 0], [widthMm, bodyHeight, depthMm]),
  ];
};

/** A media unit: a low carcass with a front split into two doors. */
const mediaUnit: Generator = ({ widthMm, depthMm, heightMm }) => {
  const legHeight = share(heightMm, 0.14, 50, 160);
  const bodyHeight = rest(heightMm, legHeight);
  const panel = Math.min(
    share(Math.min(widthMm, depthMm), 0.06, 18, 40),
    bodyHeight * 0.2,
    widthMm * 0.2,
    depthMm * 0.4,
  );
  const doorWidth = rest(widthMm, 2 * panel) / 2;
  const doorHeight = rest(bodyHeight, 2 * panel);
  return [
    ...legs(widthMm, depthMm, legHeight, Math.min(60, panel * 2)),
    box(
      [0, legHeight + bodyHeight / 2, 0],
      [widthMm, bodyHeight, depthMm],
      "body",
    ),
    box(
      [-doorWidth / 2, legHeight + bodyHeight / 2, -(depthMm - panel) / 2],
      [doorWidth, doorHeight, panel],
      "surface",
    ),
    box(
      [doorWidth / 2, legHeight + bodyHeight / 2, -(depthMm - panel) / 2],
      [doorWidth, doorHeight, panel],
      "surface",
    ),
  ];
};

/** Every family, by name. 4.10 asks for roughly twenty. */
export const FAMILIES: Record<string, Generator> = {
  sofa_3seat: seating(0.11),
  sofa_sectional_L: sectional,
  loveseat: seating(0.13),
  armchair: seating(0.18),
  accent_chair: chairUpright,
  dining_chair: chairUpright,
  office_chair: officeChair,
  coffee_table_rect: tableRect,
  coffee_table_round: tableRound,
  side_table: tableRect,
  console_table: tableRect,
  dining_table_rect: tableRect,
  dining_table_round: tableRound,
  desk: tableRect,
  tv_stand: mediaUnit,
  bookshelf_open: shelving(false),
  cabinet_closed: shelving(true),
  dresser: drawers,
  nightstand: drawers,
  bed_frame_platform: bed(false),
  bed_frame_headboard: bed(true),
  rug_rect: rug,
  floor_lamp_arc: lampArc,
  bench: block,
  ottoman: block,
};

/**
 * Families whose footprint is inscribed in the stated rectangle rather than
 * filling it -- anything radially symmetric.
 *
 * Worth naming because it changes what "the proxy fills its footprint" means:
 * a round table in a 2000 x 900 slot is 900 across, and asking it to span
 * 2000 would be asking a circle to be a rectangle.
 */
export const ROUND_FAMILIES = new Set([
  "coffee_table_round",
  "dining_table_round",
  "floor_lamp_arc",
]);

/**
 * 4.2's categories to proxy families.
 *
 * A category with no entry falls back to a plain block, which is 4.10's
 * point: the proxy is a correctly-sized stand-in, and a category the catalog
 * invents before this file catches up should render as a box of the right
 * size rather than not render at all.
 */
export const FAMILY_FOR_CATEGORY: Record<string, string> = {
  sofa: "sofa_3seat",
  sectional: "sofa_sectional_L",
  loveseat: "loveseat",
  armchair: "armchair",
  accent_chair: "accent_chair",
  coffee_table: "coffee_table_rect",
  side_table: "side_table",
  console_table: "console_table",
  tv_stand: "tv_stand",
  bookshelf: "bookshelf_open",
  bed_frame: "bed_frame_headboard",
  nightstand: "nightstand",
  dresser: "dresser",
  desk: "desk",
  office_chair: "office_chair",
  dining_table: "dining_table_rect",
  dining_chair: "dining_chair",
  rug: "rug_rect",
  floor_lamp: "floor_lamp_arc",
  bench: "bench",
  ottoman: "ottoman",
  cabinet: "cabinet_closed",
};

export const FALLBACK_FAMILY = "ottoman";

export function familyNames(): string[] {
  return Object.keys(FAMILIES).sort();
}

export function familyForCategory(category: string): string {
  return FAMILY_FOR_CATEGORY[category] ?? FALLBACK_FAMILY;
}

export function buildFamily(family: string, dimensions: Dimensions): Proxy {
  const generator = FAMILIES[family] ?? FAMILIES[FALLBACK_FAMILY]!;
  return { family, parts: generator(dimensions) };
}
