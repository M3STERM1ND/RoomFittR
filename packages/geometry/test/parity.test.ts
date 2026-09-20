/**
 * The TypeScript half of the validator parity suite (5.5, 9.1).
 *
 * 9.1 gates "Validator parity (Python <-> TS)" on every CI run, because "the
 * AI and the editor must agree on what 'fits' means". The fixtures in
 * `fixtures/validation/` are generated from the Python validator; this file
 * asserts the TypeScript one produces byte-identical JSON for each of them.
 *
 * When this fails, the question to answer is which of the two changed and
 * which is right -- never "regenerate the fixtures until it passes", which
 * would record whatever Python does and leave the disagreement in place.
 */

import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { type Footprint } from "../src/geometry.ts";
import { type RoomModel, analyse } from "../src/room.ts";
import { type PlacedItem, reportToJson, validate } from "../src/validator.ts";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, "..", "..", "..");
const fixtureDir = join(repoRoot, "fixtures", "validation");
const roomDir = join(repoRoot, "fixtures", "rooms");

interface FixtureItem {
  id: string;
  category: string;
  center_x_mm: number;
  center_z_mm: number;
  width_mm: number;
  depth_mm: number;
  height_mm: number;
  rotation_deg?: number;
  elevation_mm?: number;
  price_cents?: number;
  against_wall_id?: string;
  price_age_days?: number;
  available?: boolean;
}

interface Fixture {
  room: string;
  items: FixtureItem[];
  budget_cents: number | null;
  enforce_budget: boolean;
  expected: unknown;
}

function readJson<T>(path: string): T {
  return JSON.parse(readFileSync(path, "utf8")) as T;
}

function toFootprint(raw: FixtureItem): Footprint {
  return {
    centerXMm: raw.center_x_mm,
    centerZMm: raw.center_z_mm,
    widthMm: raw.width_mm,
    depthMm: raw.depth_mm,
    heightMm: raw.height_mm,
    rotationDeg: raw.rotation_deg ?? 0,
    elevationMm: raw.elevation_mm ?? 0,
  };
}

function toItem(raw: FixtureItem): PlacedItem {
  return {
    id: raw.id,
    category: raw.category,
    footprint: toFootprint(raw),
    priceCents: raw.price_cents ?? 0,
    againstWallId: raw.against_wall_id ?? null,
    priceAgeDays: raw.price_age_days ?? 0,
    available: raw.available ?? true,
  };
}

const fixtureNames = readdirSync(fixtureDir)
  .filter((name) => name.endsWith(".json"))
  .sort();

test("the parity suite is not empty", () => {
  // A suite that silently collects nothing passes forever.
  assert.ok(
    fixtureNames.length >= 20,
    `only ${fixtureNames.length} parity fixtures found in ${fixtureDir}`,
  );
});

for (const name of fixtureNames) {
  test(`matches the Python validator: ${name}`, () => {
    const fixture = readJson<Fixture>(join(fixtureDir, name));
    const model = readJson<RoomModel>(join(roomDir, `${fixture.room}.json`));

    const report = validate(fixture.items.map(toItem), analyse(model), {
      budgetCents: fixture.budget_cents,
      enforceBudget: fixture.enforce_budget,
    });

    assert.deepStrictEqual(reportToJson(report), fixture.expected);
  });
}

test("both verdicts are represented", () => {
  // Fixtures that all fail would let a validator rejecting everything pass;
  // fixtures that all pass would let one accepting everything pass.
  const verdicts = new Set(
    fixtureNames.map(
      (name) =>
        (
          readJson<Fixture>(join(fixtureDir, name)).expected as {
            fits: boolean;
          }
        ).fits,
    ),
  );
  assert.deepStrictEqual([...verdicts].sort(), [false, true]);
});
