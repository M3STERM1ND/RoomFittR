"use client";

import { MotionConfig, motion } from "motion/react";
import type { ReactNode } from "react";

/**
 * Motion moment two: the section reveal.
 *
 * Fires once when the block crosses 80px into the viewport, then never again.
 *
 * Two guards:
 * - `reducedMotion="user"` lets Motion drop the transform and land the block at
 *   its final position immediately. The global CSS reduced-motion block cannot
 *   do this itself, because Motion animates inline styles rather than CSS
 *   transitions.
 * - the `reveal` class is the no-JS escape hatch. A <noscript> rule in the root
 *   layout forces it back to full opacity, so a blocked or failed bundle can
 *   never strand a whole section at opacity 0.
 */
export function Reveal({
  children,
  className = "",
  delay = 0,
}: {
  children: ReactNode;
  className?: string;
  /** Seconds. Only where two blocks should land in sequence. */
  delay?: number;
}) {
  return (
    <MotionConfig reducedMotion="user">
      <motion.div
        className={`reveal ${className}`}
        initial={{ opacity: 0, y: 16 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, margin: "-80px" }}
        transition={{ duration: 0.7, ease: [0.16, 1, 0.3, 1], delay }}
      >
        {children}
      </motion.div>
    </MotionConfig>
  );
}
