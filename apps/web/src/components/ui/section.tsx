import type { ReactNode } from "react";
import { Container } from "@/components/ui/container";

/**
 * The one place section rhythm is decided: 80 / 96 / 128 / 176 vertical,
 * straight off the locked spacing scale. Sections never bleed into each
 * other, so nothing here is ever overridden by a caller.
 */
const PAD = "py-20 md:py-24 lg:py-32 xl:py-44";

const tones = {
  base: "bg-mist-50",
  wash: "border-y border-mist-300 bg-mist-100",
  ink: "bg-hur-900 text-white",
} as const;

export function Section({
  id,
  tone = "base",
  children,
}: {
  id?: string;
  tone?: keyof typeof tones;
  children: ReactNode;
}) {
  return (
    <section id={id} className={`${tones[tone]} ${PAD}`}>
      <Container>{children}</Container>
    </section>
  );
}

/**
 * Eyebrow, heading and lead. The lead sits beside the heading on wide screens
 * rather than under it, so a section never strands its argument in the left
 * third with an empty half beside it.
 */
export function SectionHead({
  eyebrow,
  heading,
  lead,
  align = "split",
}: {
  eyebrow: string;
  heading: string;
  lead: string;
  align?: "split" | "centre";
}) {
  if (align === "centre") {
    return (
      <div className="mx-auto max-w-[44rem] text-center">
        <p className="text-label font-body font-semibold text-hur-600">
          {eyebrow}
        </p>
        <h2 className="mt-4 text-display-l text-hur-900">{heading}</h2>
        <p className="mx-auto mt-6 max-w-[38rem] text-body-l text-hur-700">
          {lead}
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6 lg:flex-row lg:items-end lg:justify-between lg:gap-16">
      <div className="lg:max-w-[38rem]">
        <p className="text-label font-body font-semibold text-hur-600">
          {eyebrow}
        </p>
        <h2 className="mt-4 text-display-l text-hur-900">{heading}</h2>
      </div>
      <p className="measure text-body-l text-hur-700 lg:max-w-[34rem] lg:text-right">
        {lead}
      </p>
    </div>
  );
}
