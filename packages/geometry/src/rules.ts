/**
 * Category placement rules in the browser (implementation-plan.md 5.3).
 *
 * The numbers come from `workers/layout/roomfittr_layout/category_rules.yaml`
 * via `scripts/generate-rules.mjs`, so there is one definition rather than
 * two that can disagree. CI fails if the generated file is stale.
 *
 * A category absent from the file still works: `DEFAULT_RULE` supplies
 * neutral values and the validator simply has less to say about it. 4.2's
 * vocabulary can grow ahead of 5.3, and a new category should degrade to
 * "no special rules" rather than break a layout mid-drag.
 */

import generated from "./category-rules.json" with { type: "json" };

export interface CategoryRule {
  readonly layer: string;
  readonly placement: string;
  readonly frontClearanceMm: number;
  readonly sideClearanceMm: number;
}

export const DEFAULT_RULE: CategoryRule = {
  layer: "floor",
  placement: "float_allowed",
  frontClearanceMm: 0,
  sideClearanceMm: 0,
};

const RULES = generated as Record<string, CategoryRule>;

export function ruleFor(category: string): CategoryRule {
  return RULES[category] ?? DEFAULT_RULE;
}

export function categories(): string[] {
  return Object.keys(RULES).sort();
}
