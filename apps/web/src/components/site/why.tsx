import { Section } from "@/components/ui/section";
import { Reveal } from "@/components/ui/reveal";

/**
 * Section six: the one argument, centred.
 *
 * Every other section is left aligned, so putting the single reason-to-exist
 * on the centre line is what makes it land. One idea, three supports, nothing
 * else on the page.
 */
const supports = [
  {
    title: "The measurement you skip",
    body: "Almost nobody measures before they buy. The listing says 210cm and the room says something you never checked.",
  },
  {
    title: "The return you cannot make",
    body: "Large furniture is the hardest thing to send back. Collection fees and restocking charges turn a bad guess into a real cost.",
  },
  {
    title: "The budget that creeps",
    body: "A room is bought one item at a time, so the total only becomes real once everything has already arrived.",
  },
];

export function Why() {
  return (
    <Section id="why" tone="wash">
      <Reveal>
        <div className="mx-auto max-w-[46rem] text-center">
          <p className="text-label font-body font-semibold text-hur-600">
            Why RoomFittr
          </p>
          <h2 className="mt-4 text-display-l text-hur-900">
            Furniture is the one big thing people buy without trying it.
          </h2>
          <p className="mx-auto mt-6 max-w-[40rem] text-body-l text-hur-700">
            You can try on a jacket and test drive a car. A sofa turns up on a
            Tuesday and either works or does not. RoomFittr moves that moment
            forward, to before you have paid for it.
          </p>
        </div>
      </Reveal>

      <Reveal className="mt-14 md:mt-16 lg:mt-20" delay={0.1}>
        <ul className="grid gap-10 border-t border-mist-300 pt-12 sm:grid-cols-3 sm:gap-8 lg:gap-12">
          {supports.map((s) => (
            <li key={s.title}>
              <h3 className="text-display-m text-hur-900">{s.title}</h3>
              <p className="mt-3 text-body-m text-hur-700">{s.body}</p>
            </li>
          ))}
        </ul>
      </Reveal>
    </Section>
  );
}
