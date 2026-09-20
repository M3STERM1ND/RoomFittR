/**
 * Generate `src/category-rules.json` from `category_rules.yaml`.
 *
 * The YAML in `workers/layout/roomfittr_layout/` is the single definition of
 * the placement rules (5.3). The browser validator needs the same numbers,
 * and shipping a YAML parser to do it would be a second parser to keep in
 * step. So the file is converted here and committed, exactly as the JSON
 * Schemas are, with `--check` failing CI when the committed copy is stale.
 *
 * Only the fields the TypeScript validator actually reads are emitted. The
 * solver's fields (relations, anchors, gap bands) stay on the Python side,
 * because the solver does too.
 */

import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const yamlPath = join(
  here,
  "..",
  "..",
  "..",
  "workers",
  "layout",
  "roomfittr_layout",
  "category_rules.yaml",
);
const outputPath = join(here, "..", "src", "category-rules.json");

/**
 * A deliberately small YAML reader.
 *
 * `category_rules.yaml` is two levels deep with scalar and flow-sequence
 * values, and nothing else. A dependency would be a bigger commitment than
 * the file warrants, and this throws on anything it does not understand
 * rather than guessing -- so a future rule using a YAML feature this cannot
 * read fails the build instead of silently losing a clearance.
 */
function parseSimpleYaml(text) {
  const root = {};
  let current = null;

  const lines = text.split(/\r?\n/);
  for (const [index, raw] of lines.entries()) {
    const withoutComment = raw.replace(/\s+#.*$/, "");
    if (
      withoutComment.trim() === "" ||
      withoutComment.trimStart().startsWith("#")
    )
      continue;

    const indent = withoutComment.length - withoutComment.trimStart().length;
    const line = withoutComment.trim();

    if (indent === 0) {
      if (!line.endsWith(":")) {
        throw new Error(
          `line ${index + 1}: expected "category:", got ${JSON.stringify(line)}`,
        );
      }
      current = {};
      root[line.slice(0, -1)] = current;
      continue;
    }

    if (current === null)
      throw new Error(`line ${index + 1}: value before any category`);
    const separator = line.indexOf(":");
    if (separator < 0)
      throw new Error(`line ${index + 1}: no key in ${JSON.stringify(line)}`);
    const key = line.slice(0, separator).trim();
    const value = line.slice(separator + 1).trim();
    current[key] = parseScalar(value, index + 1);
  }
  return root;
}

function parseScalar(value, lineNumber) {
  if (value === "") return null;
  if (value === "true") return true;
  if (value === "false") return false;
  if (value.startsWith("[") && value.endsWith("]")) {
    const inner = value.slice(1, -1).trim();
    if (inner === "") return [];
    return inner.split(",").map((part) => parseScalar(part.trim(), lineNumber));
  }
  if (/^-?\d+(\.\d+)?$/.test(value)) return Number(value);
  // Expressions like `sill-50` are notes for a human, not numbers.
  return value;
}

const parsed = parseSimpleYaml(readFileSync(yamlPath, "utf8"));

const rules = {};
for (const [category, body] of Object.entries(parsed)) {
  rules[category] = {
    layer: typeof body.layer === "string" ? body.layer : "floor",
    placement:
      typeof body.placement === "string" ? body.placement : "float_allowed",
    frontClearanceMm:
      typeof body.front_clearance_mm === "number" ? body.front_clearance_mm : 0,
    sideClearanceMm:
      typeof body.side_clearance_mm === "number" ? body.side_clearance_mm : 0,
  };
}

const generated = `${JSON.stringify(rules, null, 2)}\n`;

if (process.argv.includes("--check")) {
  const existing = readFileSync(outputPath, "utf8");
  if (existing !== generated) {
    console.error(
      "category-rules.json is stale. Run `pnpm --filter @roomfittr/geometry gen`.",
    );
    process.exit(1);
  }
  console.log("category-rules.json is current");
} else {
  writeFileSync(outputPath, generated);
  console.log(`wrote ${Object.keys(rules).length} categories`);
}
