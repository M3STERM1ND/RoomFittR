import { Section } from "@/components/ui/section";
import { Reveal } from "@/components/ui/reveal";

/**
 * Section seven: the questions, answered straight.
 *
 * Built on native <details>, so it opens without JavaScript, is keyboard
 * operable and is announced correctly, with no accordion library added for
 * something the browser already does.
 *
 * The limitations answer is deliberately honest and comes from
 * implementation-plan.md section 3.11. A landing page that hides them just
 * moves the disappointment later.
 */
const faqs = [
  {
    q: "What do I actually have to record?",
    a: "One room, twenty seconds to three minutes, on a normal phone camera. Turn the lights on, walk the perimeter slowly facing the walls, and try to keep the line where the floor meets the wall in shot. That junction is what the room geometry is built from.",
  },
  {
    q: "How accurate are the measurements?",
    a: "Accurate enough to tell you whether a 210cm sofa fits a wall, and we tell you how confident we are rather than presenting a guess as fact. Type in one wall you have measured yourself and everything else is calibrated against it.",
  },
  {
    q: "How long does it take?",
    a: "Usually about six minutes, sometimes longer for a large or awkward room. It runs in the background, so you can close the tab and come back to it.",
  },
  {
    q: "Which rooms does it struggle with?",
    a: "Large mirrors and glass, very dark rooms, curved walls, sloped or attic ceilings, and open-plan spaces with no clear boundary. You get a warning when the room is one of these, rather than a confident wrong answer.",
  },
  {
    q: "Do I need an account?",
    a: "No. You can scan a room and furnish it without signing up. Signing in with Google only adds the ability to come back to a saved room later.",
  },
  {
    q: "What happens to my video?",
    a: "It is kept so that you can revisit the room without recording it again, it is visible only to you, and you can delete it along with everything derived from it at any time.",
  },
];

export function Faq() {
  return (
    <Section id="faq">
      <div className="grid gap-12 lg:grid-cols-12 lg:gap-16">
        <Reveal className="lg:col-span-4">
          <p className="text-label font-body font-semibold text-hur-600">
            Questions
          </p>
          <h2 className="mt-4 text-display-l text-hur-900">
            The things worth asking first.
          </h2>
          <p className="measure mt-6 text-body-m text-hur-700">
            Including what it is not good at. You will find that out eventually
            anyway, and it is cheaper to find out now.
          </p>
        </Reveal>

        <Reveal className="lg:col-span-8" delay={0.1}>
          <ul className="border-t border-hur-200">
            {faqs.map((item) => (
              <li key={item.q} className="border-b border-hur-200">
                <details className="group">
                  <summary className="flex cursor-pointer list-none items-center justify-between gap-6 py-6 text-left [&::-webkit-details-marker]:hidden">
                    <h3 className="font-display text-[1.1875rem] tracking-[-0.01em] text-hur-900 md:text-[1.3125rem]">
                      {item.q}
                    </h3>
                    {/* A plus that becomes a minus. Two rules, no icon set. */}
                    <span
                      aria-hidden="true"
                      className="relative grid size-6 shrink-0 place-items-center"
                    >
                      <span className="absolute h-px w-3.5 bg-hur-600" />
                      <span className="absolute h-3.5 w-px bg-hur-600 transition-opacity duration-300 ease-calm group-open:opacity-0 motion-reduce:transition-none" />
                    </span>
                  </summary>
                  <p className="measure pb-7 text-body-m text-hur-700">
                    {item.a}
                  </p>
                </details>
              </li>
            ))}
          </ul>
        </Reveal>
      </div>
    </Section>
  );
}
