import { Section, SectionHead } from "@/components/ui/section";
import { Reveal } from "@/components/ui/reveal";
import { RoomCanvas } from "@/components/three/room-canvas";

/**
 * Section three: what a finished scan actually looks like.
 *
 * One idea only. The room carries it, so the copy stays out of the way and
 * the canvas runs the full content width rather than sitting in a column with
 * an empty half beside it.
 */
export function RoomPreview() {
  return (
    <Section id="preview" tone="wash">
      <Reveal>
        <SectionHead
          eyebrow="What you get back"
          heading="Your room, cleared out and furnished."
          lead="The walls, the window and the floor come straight from your video. Everything standing in the room is a real product with a real price, sized against your actual measurements."
        />
      </Reveal>

      <Reveal className="mt-12 md:mt-14 lg:mt-16" delay={0.1}>
        <RoomCanvas />

        <div className="mt-5 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between sm:gap-8">
          <p className="flex items-center gap-2.5 text-body-s text-hur-600">
            <span
              aria-hidden="true"
              className="block size-2.5 shrink-0 rounded-[3px] bg-mist-400"
            />
            Blue marks the piece you have open. Tap any item to go to the shop
            page.
          </p>
          <p className="text-body-s text-hur-600">
            Move your cursor across the room to shift the view.
          </p>
        </div>
      </Reveal>
    </Section>
  );
}
