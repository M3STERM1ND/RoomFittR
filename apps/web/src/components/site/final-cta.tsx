import { Container } from "@/components/ui/container";
import { ButtonLink } from "@/components/ui/button";
import { Reveal } from "@/components/ui/reveal";

/**
 * Section eight: one action.
 *
 * The only hur-900 band above the footer, which is what makes it read as the
 * end of the argument. Centred, because there is nothing here to weigh
 * against it.
 */
export function FinalCta() {
  return (
    <section
      id="scan"
      className="bg-hur-900 pt-20 pb-16 md:pt-24 md:pb-20 lg:pt-32 lg:pb-24 xl:pt-40"
    >
      <Container>
        <Reveal>
          <div className="mx-auto max-w-[42rem] text-center">
            <h2 className="text-display-l text-white">
              Find out what fits before you pay for it.
            </h2>
            <p className="mx-auto mt-6 max-w-[34rem] text-body-l text-mist-200">
              Record one room, set one budget, and see the whole thing furnished
              with products you can actually buy.
            </p>

            {/* One action. The second button that wants to live here would
                only point back up the page the reader has just finished. */}
            <div className="mt-10 flex justify-center">
              <ButtonLink href="#scan" variant="inverse">
                Scan a room
              </ButtonLink>
            </div>

            <p className="mt-8 text-body-s text-mist-400">
              No account needed. Delete anything you upload whenever you want.
            </p>
          </div>
        </Reveal>
      </Container>
    </section>
  );
}
