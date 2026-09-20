/**
 * `@roomfittr/furniture-proxies` -- low-poly stand-ins for catalog products.
 *
 * 4.10: retailers almost never publish usable 3D models, so V1 builds a proxy
 * from the product's own width, depth and height. The product photo stays the
 * primary "what does it look like" signal (R13); the proxy's job is to be
 * exactly the right size in the room.
 */

export * from "./types.ts";
export * from "./families.ts";

import { buildFamily, familyForCategory } from "./families.ts";
import type { Dimensions, Proxy } from "./types.ts";

/**
 * The proxy for a catalog product.
 *
 * Takes the category rather than the family so a caller with a
 * `layout_candidates` row does not have to know the mapping. A category the
 * mapping has never heard of still renders, as a correctly-sized block --
 * 4.2's vocabulary can grow ahead of this file, and an unrenderable item
 * would be worse than a plain one.
 */
export function proxyForCategory(
  category: string,
  dimensions: Dimensions,
): Proxy {
  return buildFamily(familyForCategory(category), dimensions);
}
