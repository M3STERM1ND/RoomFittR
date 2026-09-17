import { Section, SectionHead } from "@/components/ui/section";
import { Reveal } from "@/components/ui/reveal";

/** Every claim here maps to a V1 feature in masterplan.md section 3. */
const features = [
  {
    title: "Measured, not guessed",
    body: "Dimensions come from the video. Type in one wall you have actually measured and the rest calibrates to it, with the confidence shown rather than hidden.",
  },
  {
    title: "Cleared out first",
    body: "Whatever is already in the room is detected and taken out, so you are furnishing an empty space instead of arguing with the sofa you own.",
  },
  {
    title: "A layout, not a list",
    body: "You get a complete starter room straight away: real products, placed where they fit, chosen against the budget you set.",
  },
  {
    title: "Swap and re-price live",
    body: "Change any piece and the total moves with it. The budget is one number you set once, and it is visible the whole time you are editing.",
  },
  {
    title: "Tap through to buy",
    body: "Every item is a real product from a real retailer. Tap it in the room to see the details and go straight to the page that sells it.",
  },
  {
    title: "No account to try it",
    body: "Scan a room without signing up. Sign in with Google only if you want the room saved, and delete anything you have uploaded whenever you like.",
  },
];

export function Features() {
  return (
    <Section id="features">
      <Reveal>
        <SectionHead
          eyebrow="What it does"
          heading="Enough to decide, nothing you have to learn."
          lead="Six things the app does. There is no modelling, no drag-and-drop canvas, and nothing to set up before you see a result."
        />
      </Reveal>

      <Reveal className="mt-14 md:mt-16 lg:mt-20" delay={0.1}>
        <ul className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3 lg:gap-8">
          {features.map((f) => (
            <li
              key={f.title}
              className="rounded-panel border border-hur-200 bg-white p-7 lg:p-8"
            >
              <span
                aria-hidden="true"
                className="block size-2.5 rounded-full bg-mist-400"
              />
              <h3 className="mt-6 text-display-m text-hur-900">{f.title}</h3>
              <p className="mt-3 text-body-m text-hur-700">{f.body}</p>
            </li>
          ))}
        </ul>
      </Reveal>
    </Section>
  );
}
