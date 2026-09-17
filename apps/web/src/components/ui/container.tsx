import type { ReactNode } from "react";

/**
 * The single horizontal frame for the whole page.
 * Gutters follow the locked scale: 20 / 32 / 40 / 48.
 */
export function Container({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`mx-auto w-full max-w-[1280px] px-5 md:px-8 lg:px-10 xl:px-12 ${className}`}
    >
      {children}
    </div>
  );
}
