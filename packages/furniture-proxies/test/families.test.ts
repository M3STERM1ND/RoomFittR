/**
 * Every proxy family, at a spread of real product sizes (4.10).
 *
 * The property that matters is containment. The layout engine reserved
 * exactly `width x depth` of floor for an item and cleared exactly `height`
 * of air above it; a proxy that spills outside those bounds collides with
 * something the validator certified as clear, and the user sees furniture
 * inside a wall in a layout the app called valid.
 *
 * So these tests are mostly one assertion applied to twenty-five generators
 * at a dozen sizes each. That is the right shape: the generators are small
 * and similar, and the way they go wrong is all the same way.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  FAMILY_FOR_CATEGORY,
  MAX_TRIANGLES_PER_ITEM,
  ROUND_FAMILIES,
  type Dimensions,
  buildFamily,
  extent,
  familyForCategory,
  familyNames,
  proxyForCategory,
  triangleCount,
} from "../src/index.ts";

/** Sizes spanning what the catalog's plausibility bands allow (4.7). */
const SIZES: Dimensions[] = [
  { widthMm: 400, depthMm: 400, heightMm: 400 },
  { widthMm: 600, depthMm: 500, heightMm: 750 },
  { widthMm: 900, depthMm: 400, heightMm: 1800 },
  { widthMm: 1200, depthMm: 600, heightMm: 450 },
  { widthMm: 1550, depthMm: 2100, heightMm: 1100 },
  { widthMm: 2000, depthMm: 900, heightMm: 800 },
  { widthMm: 2400, depthMm: 1700, heightMm: 12 },
  { widthMm: 3000, depthMm: 1600, heightMm: 850 },
  { widthMm: 4000, depthMm: 1100, heightMm: 900 },
];

// Floating-point slack only. A millimetre of real overflow is still a bug.
const EPSILON = 1e-6;

for (const family of familyNames()) {
  test(`${family}: every part stays inside the stated dimensions`, () => {
    for (const size of SIZES) {
      const proxy = buildFamily(family, size);
      assert.ok(
        proxy.parts.length > 0,
        `${family} produced no geometry at ${size.widthMm}mm`,
      );

      const { min, max } = extent(proxy);
      const where = `${family} at ${size.widthMm}x${size.depthMm}x${size.heightMm}`;

      assert.ok(
        min[0] >= -size.widthMm / 2 - EPSILON,
        `${where}: overflows -X by ${-size.widthMm / 2 - min[0]}`,
      );
      assert.ok(
        max[0] <= size.widthMm / 2 + EPSILON,
        `${where}: overflows +X by ${max[0] - size.widthMm / 2}`,
      );
      assert.ok(
        min[2] >= -size.depthMm / 2 - EPSILON,
        `${where}: overflows -Z by ${-size.depthMm / 2 - min[2]}`,
      );
      assert.ok(
        max[2] <= size.depthMm / 2 + EPSILON,
        `${where}: overflows +Z by ${max[2] - size.depthMm / 2}`,
      );
    }
  });

  test(`${family}: nothing sinks below the floor or pokes through the ceiling`, () => {
    for (const size of SIZES) {
      const { min, max } = extent(buildFamily(family, size));
      const where = `${family} at height ${size.heightMm}`;
      assert.ok(
        min[1] >= -EPSILON,
        `${where}: sinks ${-min[1]} mm below the floor`,
      );
      assert.ok(
        max[1] <= size.heightMm + EPSILON,
        `${where}: exceeds its height by ${max[1] - size.heightMm}`,
      );
    }
  });

  test(`${family}: stays inside the triangle budget`, () => {
    for (const size of SIZES) {
      const count = triangleCount(buildFamily(family, size));
      assert.ok(
        count <= MAX_TRIANGLES_PER_ITEM,
        `${family} is ${count} triangles, over 4.10's ${MAX_TRIANGLES_PER_ITEM}`,
      );
    }
  });

  test(`${family}: every part has a positive size`, () => {
    // A zero or negative dimension renders as nothing or as an inverted box,
    // and reads as a missing piece of furniture rather than as a bug.
    for (const size of SIZES) {
      for (const part of buildFamily(family, size).parts) {
        for (const axis of part.size) {
          assert.ok(
            axis > 0,
            `${family} has a part sized ${part.size.join("x")} at ${size.widthMm}mm`,
          );
        }
      }
    }
  });

  test(`${family}: is deterministic`, () => {
    // The viewer rebuilds proxies on every load; furniture that changed shape
    // between two views of the same layout would read as a bug in the scan.
    const size = SIZES[5]!;
    assert.deepStrictEqual(
      buildFamily(family, size),
      buildFamily(family, size),
    );
  });

  test(`${family}: fills a reasonable share of its footprint`, () => {
    // Containment alone would be satisfied by a single tiny box. This is the
    // other half: the proxy has to look like the thing it stands in for.
    const size = SIZES[5]!;
    const { min, max } = extent(buildFamily(family, size));

    // A radially symmetric family is inscribed in the stated rectangle, so
    // its span is the shorter side by definition.
    const inscribed = ROUND_FAMILIES.has(family);
    const wantedX = inscribed
      ? Math.min(size.widthMm, size.depthMm) * 0.8
      : size.widthMm * 0.5;
    const wantedZ = inscribed
      ? Math.min(size.widthMm, size.depthMm) * 0.8
      : size.depthMm * 0.5;

    assert.ok(
      max[0] - min[0] >= wantedX,
      `${family} only spans ${max[0] - min[0]} mm of a wanted ${wantedX} mm`,
    );
    assert.ok(
      max[2] - min[2] >= wantedZ,
      `${family} only spans ${max[2] - min[2]} mm deep of a wanted ${wantedZ} mm`,
    );
  });
}

test("every category in the layout vocabulary maps to a real family", () => {
  // 4.2's vocabulary is closed and 4.10 promises a proxy for all of it. A
  // mapping pointing at a family that does not exist would silently fall
  // through to the block.
  const names = new Set(familyNames());
  for (const [category, family] of Object.entries(FAMILY_FOR_CATEGORY)) {
    assert.ok(
      names.has(family),
      `${category} maps to unknown family ${family}`,
    );
  }
});

test("the layout engine's full category vocabulary is covered", () => {
  const vocabulary = [
    "sofa",
    "sectional",
    "loveseat",
    "armchair",
    "accent_chair",
    "coffee_table",
    "side_table",
    "console_table",
    "tv_stand",
    "bookshelf",
    "bed_frame",
    "nightstand",
    "dresser",
    "desk",
    "office_chair",
    "dining_table",
    "dining_chair",
    "rug",
    "floor_lamp",
    "bench",
    "ottoman",
    "cabinet",
  ];
  for (const category of vocabulary) {
    assert.ok(
      category in FAMILY_FOR_CATEGORY,
      `${category} has no proxy family; it would render as a plain block`,
    );
  }
});

test("an unknown category still renders", () => {
  // 4.2's vocabulary can grow ahead of this file. An unrenderable item would
  // be worse than a plain one.
  const proxy = proxyForCategory("chaise_longue", {
    widthMm: 1600,
    depthMm: 700,
    heightMm: 800,
  });
  assert.ok(proxy.parts.length > 0);
  assert.equal(familyForCategory("chaise_longue"), "ottoman");
});

test("a rug is a single thin slab", () => {
  // 4.5 defaults a rug's height to 10 mm, and the validator treats it as a
  // floor covering things sit on top of. A rug with legs would be absurd.
  const proxy = proxyForCategory("rug", {
    widthMm: 2000,
    depthMm: 1400,
    heightMm: 10,
  });
  assert.equal(proxy.parts.length, 1);
  assert.ok(proxy.parts[0]!.size[1] <= 10 + EPSILON);
});

test("a sofa's back sits behind its seat", () => {
  // 2.4 puts an item's front at -Z, and the solver rotates a sofa so its
  // front faces the room. A proxy with the back at -Z would face every sofa
  // into the wall while passing every other test here.
  const proxy = proxyForCategory("sofa", {
    widthMm: 2000,
    depthMm: 900,
    heightMm: 800,
  });
  const tallest = proxy.parts.reduce((best, part) =>
    part.size[1] > best.size[1] ? part : best,
  );
  assert.ok(
    tallest.center[2] > 0,
    `the tallest part of a sofa is at z=${tallest.center[2]}; the back belongs at +Z`,
  );
});

test("a bed with a headboard puts it at the head", () => {
  const proxy = proxyForCategory("bed_frame", {
    widthMm: 1550,
    depthMm: 2100,
    heightMm: 1100,
  });
  const tallest = proxy.parts.reduce((best, part) =>
    part.size[1] > best.size[1] ? part : best,
  );
  assert.ok(tallest.center[2] > 0, "the headboard belongs at +Z");
});
