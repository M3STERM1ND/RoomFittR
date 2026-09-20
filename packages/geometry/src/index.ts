/**
 * `@roomfittr/geometry` -- the browser half of the layout contract.
 *
 * 5.5 requires the validator to exist twice and agree with itself: this
 * package is what the editor calls on every pointer-move, and
 * `workers/layout/` is what the API calls on save. `fixtures/validation/`
 * pins the two together in CI.
 */

export * from "./geometry.ts";
export * from "./room.ts";
export * from "./placer.ts";
export * from "./rules.ts";
export * from "./validator.ts";
