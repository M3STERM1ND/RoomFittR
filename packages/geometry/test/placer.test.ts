/**
 * The single-item placer (5.6, 7.1).
 *
 * The property that matters is that whatever it proposes, the validator
 * accepts. The placer exists so the user gets a pose instantly on "Add"; if
 * that pose then turned up red, the feature would be worse than nothing.
 *
 * So most of these tests place something and then run the real validator over
 * the result, rather than re-asserting the placer's own rules back at it.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { type Footprint } from "../src/geometry.ts";
import { placeOne } from "../src/placer.ts";
import { type RoomModel, analyse } from "../src/room.ts";
import { type PlacedItem, validate } from "../src/validator.ts";

const here = dirname(fileURLToPath(import.meta.url));
const roomDir = join(here, "..", "..", "..", "fixtures", "rooms");

function room(name: string) {
  return analyse(
    JSON.parse(
      readFileSync(join(roomDir, `${name}.json`), "utf8"),
    ) as RoomModel,
  );
}

function item(
  id: string,
  category: string,
  footprint: Footprint,
  againstWallId: string | null = null,
): PlacedItem {
  return {
    id,
    category,
    footprint,
    priceCents: 0,
    againstWallId,
    priceAgeDays: 0,
    available: true,
  };
}

const ROOMS = [
  "rectangular-living",
  "small-bedroom",
  "l-shaped-living",
  "open-plan-boundary",
  "many-openings",
  "narrow-room",
];

for (const name of ROOMS) {
  test(`${name}: a placed sofa passes the validator`, () => {
    const analysis = room(name);
    const placement = placeOne(
      { category: "sofa", widthMm: 1800, depthMm: 880, heightMm: 800 },
      analysis,
      [],
    );
    if (placement === null) return; // A room too small for a sofa is a fair answer.

    const report = validate(
      [item("new", "sofa", placement.footprint, placement.againstWallId)],
      analysis,
    );
    assert.ok(
      report.fits,
      `${name}: ${[...report.items.values()]
        .flat()
        .map((v) => v.message)
        .join("; ")}`,
    );
  });

  test(`${name}: adding to an occupied room still passes the validator`, () => {
    const analysis = room(name);
    const first = placeOne(
      { category: "sofa", widthMm: 1800, depthMm: 880, heightMm: 800 },
      analysis,
      [],
    );
    if (first === null) return;
    const existing = [item("a", "sofa", first.footprint, first.againstWallId)];

    const second = placeOne(
      { category: "bookshelf", widthMm: 800, depthMm: 320, heightMm: 1800 },
      analysis,
      existing,
    );
    if (second === null) return;

    const report = validate(
      [
        ...existing,
        item("b", "bookshelf", second.footprint, second.againstWallId),
      ],
      analysis,
    );
    assert.ok(
      report.fits,
      `${name}: ${[...report.items.values()]
        .flat()
        .map((v) => v.message)
        .join("; ")}`,
    );
  });
}

test("a wall-preferring item is put against a wall", () => {
  // 5.3 marks a sofa `against_wall_preferred`. One floating in the middle of
  // an empty room is a valid layout and a wrong one.
  const analysis = room("rectangular-living");
  const placement = placeOne(
    { category: "sofa", widthMm: 1800, depthMm: 880, heightMm: 800 },
    analysis,
    [],
  );
  assert.ok(placement);
  assert.notEqual(placement.againstWallId, null);
});

test("a sofa against a wall faces into the room", () => {
  // The rotation is derived rather than guessed; the reflexive answer is 180
  // degrees out and would face every added sofa at the wall.
  const analysis = room("rectangular-living");
  const placement = placeOne(
    { category: "sofa", widthMm: 1800, depthMm: 880, heightMm: 800 },
    analysis,
    [],
  );
  assert.ok(placement);
  const report = validate(
    [item("new", "sofa", placement.footprint, placement.againstWallId)],
    analysis,
  );
  const codes = [...report.items.values()].flat().map((v) => v.code);
  assert.ok(
    !codes.includes("S4_BACK_NOT_TO_WALL"),
    "the sofa is facing the wall",
  );
});

test("it does not block a doorway", () => {
  const analysis = room("many-openings");
  const placement = placeOne(
    { category: "bookshelf", widthMm: 900, depthMm: 350, heightMm: 1800 },
    analysis,
    [],
  );
  assert.ok(placement);
  const report = validate(
    [item("new", "bookshelf", placement.footprint, placement.againstWallId)],
    analysis,
  );
  const codes = [...report.items.values()].flat().map((v) => v.code);
  assert.ok(!codes.includes("H4_DOOR_KEEPOUT"));
});

test("it does not seal the room in half", () => {
  // The narrow room is where this bites: its walkable band is barely wider
  // than one person, so a badly placed item disconnects the two ends.
  const analysis = room("narrow-room");
  const placement = placeOne(
    { category: "bookshelf", widthMm: 350, depthMm: 1600, heightMm: 1800 },
    analysis,
    [],
  );
  if (placement === null) return;
  const report = validate(
    [item("new", "bookshelf", placement.footprint, placement.againstWallId)],
    analysis,
  );
  assert.ok(report.fits, report.violations.map((v) => v.message).join("; "));
});

test("an item that cannot fit returns null rather than a bad pose", () => {
  // The UI needs "there is no room for this", not a red outline a moment
  // after the item appears.
  const analysis = room("small-bedroom");
  const placement = placeOne(
    { category: "sofa", widthMm: 4200, depthMm: 1100, heightMm: 900 },
    analysis,
    [],
  );
  assert.equal(placement, null);
});

test("an item taller than the ceiling returns null", () => {
  const analysis = room("rectangular-living");
  assert.equal(
    placeOne(
      { category: "bookshelf", widthMm: 800, depthMm: 320, heightMm: 9000 },
      analysis,
      [],
    ),
    null,
  );
});

test("a rug may be placed over a doorway and under furniture", () => {
  // 5.5 exempts floor coverings from H2 and H4: you can walk on a rug.
  const analysis = room("rectangular-living");
  const sofa = placeOne(
    { category: "sofa", widthMm: 1800, depthMm: 880, heightMm: 800 },
    analysis,
    [],
  );
  assert.ok(sofa);
  const existing = [item("a", "sofa", sofa.footprint, sofa.againstWallId)];

  const rug = placeOne(
    { category: "rug", widthMm: 2000, depthMm: 1400, heightMm: 10 },
    analysis,
    existing,
  );
  assert.ok(rug);
  const report = validate(
    [...existing, item("r", "rug", rug.footprint)],
    analysis,
  );
  assert.ok(report.fits, report.violations.map((v) => v.message).join("; "));
});

test("it is deterministic", () => {
  // "Add" twice in the same room must not put the item in two places, or
  // undo and redo stop being the same operation.
  const analysis = room("rectangular-living");
  const request = {
    category: "armchair" as const,
    widthMm: 800,
    depthMm: 820,
    heightMm: 950,
  };
  const first = placeOne(request, analysis, []);
  const second = placeOne(request, analysis, []);
  assert.deepStrictEqual(first, second);
});

test("it prefers a pose near where the user was looking", () => {
  // A small tie-break, not an override: an item appearing behind the camera
  // reads as the app ignoring the user.
  const analysis = room("rectangular-living");
  const request = {
    category: "ottoman" as const,
    widthMm: 600,
    depthMm: 600,
    heightMm: 420,
  };

  const left = placeOne({ ...request, near: [-2000, 0] }, analysis, []);
  const right = placeOne({ ...request, near: [2000, 0] }, analysis, []);
  assert.ok(left && right);
  assert.ok(
    left.footprint.centerXMm < right.footprint.centerXMm,
    `hint ignored: ${left.footprint.centerXMm} vs ${right.footprint.centerXMm}`,
  );
});

test("filling a room one item at a time never produces an invalid layout", () => {
  // The realistic use: the user keeps pressing Add. Every intermediate state
  // has to be one the validator accepts, because every one of them is a state
  // the user sees.
  const analysis = room("rectangular-living");
  const wanted: Array<[string, number, number, number]> = [
    ["sofa", 1800, 880, 800],
    ["coffee_table", 1000, 550, 420],
    ["armchair", 800, 820, 950],
    ["bookshelf", 800, 320, 1800],
    ["floor_lamp", 400, 400, 1650],
    ["ottoman", 600, 600, 420],
  ];

  const placed: PlacedItem[] = [];
  for (const [
    index,
    [category, widthMm, depthMm, heightMm],
  ] of wanted.entries()) {
    const placement = placeOne(
      { category, widthMm, depthMm, heightMm },
      analysis,
      placed,
    );
    if (placement === null) continue;
    placed.push(
      item(`i${index}`, category, placement.footprint, placement.againstWallId),
    );

    const report = validate(placed, analysis);
    assert.ok(
      report.fits,
      `after adding ${category}: ` +
        [
          ...report.violations.map((v) => v.message),
          ...[...report.items.values()].flat().map((v) => v.message),
        ]
          .filter(Boolean)
          .join("; "),
    );
  }
  assert.ok(placed.length >= 3, `only placed ${placed.length} items`);
});
