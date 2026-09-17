import { Section, SectionHead } from "@/components/ui/section";
import { Reveal } from "@/components/ui/reveal";

/**
 * Section four: the whole process, in three steps.
 *
 * Numbers are from implementation-plan.md sections 3.2 and 3.9, not invented.
 * Three across on desktop, a vertical timeline on mobile, per design-system
 * section 8. Stacked cards would lose the sense of order, which is the only
 * thing this section is arguing.
 */
const steps = [
  {
    title: "Walk the room once",
    body: "Hold your phone at chest height and walk the perimeter slowly, facing the walls. Keep the line where the floor meets the wall in shot. Twenty seconds is enough, three minutes is the limit.",
  },
  {
    title: "We rebuild it",
    body: "The video comes back as a measured 3D room with the old furniture taken out. It takes about six minutes, so you can close the tab and come back. Give it one real wall measurement and everything else calibrates to it.",
  },
  {
    title: "Furnish it to a budget",
    body: "Set one number. RoomFittr fills the room with real products that fit the space and add up to less than it. Swap anything you do not like and the total moves with you.",
  },
];

export function HowItWorks() {
  return (
    <Section id="how-it-works">
      <Reveal>
        <SectionHead
          eyebrow="How it works"
          heading="Three steps, one walk around the room."
          lead="No tape measure, no floor plan, no depth sensor. A phone camera and a slow lap of the room is the whole input."
        />
      </Reveal>

      <Reveal className="mt-14 md:mt-16 lg:mt-20" delay={0.1}>
        <ol className="grid gap-12 lg:grid-cols-3 lg:gap-14">
          {steps.map((step, i) => (
            <li key={step.title} className="relative pl-16 lg:pl-0 lg:pt-16">
              {/* The timeline rule. Runs down the list on mobile and along the
                  top of the row on desktop, and never trails off the last
                  step into nothing. */}
              {i < steps.length - 1 && (
                <span
                  aria-hidden="true"
                  className="absolute top-12 bottom-[-3rem] left-[1.375rem] w-px bg-mist-300 lg:top-[1.375rem] lg:right-[-3.5rem] lg:bottom-auto lg:left-12 lg:h-px lg:w-auto"
                />
              )}

              <span
                aria-hidden="true"
                className="absolute top-0 left-0 grid size-11 place-items-center rounded-full border border-mist-300 bg-mist-50 font-display text-[1.0625rem] text-hur-900"
              >
                {i + 1}
              </span>

              <h3 className="text-display-m text-hur-900">{step.title}</h3>
              <p className="measure mt-3 text-body-m text-hur-700">
                {step.body}
              </p>
            </li>
          ))}
        </ol>
      </Reveal>
    </Section>
  );
}
