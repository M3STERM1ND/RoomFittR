import type { CSSProperties } from "react";
import { Container } from "@/components/ui/container";
import { ButtonLink } from "@/components/ui/button";
import { RoomPlanCard } from "@/components/site/room-plan-card";

const facts = [
  "About six minutes a scan",
  "Runs in your browser",
  "No account needed to try",
];

/** 70ms between each entering element, per the motion spec. */
const delay = (step: number): CSSProperties =>
  ({ "--rise-delay": `${step * 70}ms` }) as CSSProperties;

export function Hero() {
  return (
    <section
      id="top"
      className="pt-14 pb-20 md:pt-20 md:pb-32 lg:pt-24 lg:pb-40"
    >
      <Container>
        <div className="grid grid-cols-1 items-center gap-12 md:gap-14 lg:grid-cols-12 lg:gap-16">
          {/* --- the argument ------------------------------------------- */}
          <div className="min-w-0 lg:col-span-7">
            <p
              className="rise text-label font-body font-semibold text-hur-600"
              style={delay(0)}
            >
              Room scanning and furniture fitting
            </p>

            <h1
              className="rise mt-5 text-display-xl text-hur-900"
              style={delay(1)}
            >
              Buy furniture that actually fits.
            </h1>

            <p
              className="rise measure-lead mt-6 text-body-l text-hur-700"
              style={delay(2)}
            >
              Walk around your room once with your phone. RoomFittr turns that
              video into a measured 3D space, clears out what is already there,
              and fills it with real products you can buy, inside the budget you
              set.
            </p>

            <div
              className="rise mt-9 flex flex-wrap items-center gap-4"
              style={delay(3)}
            >
              <ButtonLink href="#scan">Scan a room</ButtonLink>
              <ButtonLink href="#how-it-works" variant="secondary">
                See how it works
              </ButtonLink>
            </div>

            <ul
              className="rise mt-10 flex flex-wrap items-center gap-x-6 gap-y-3 border-t border-mist-300 pt-6 lg:gap-x-8"
              style={delay(4)}
            >
              {facts.map((fact) => (
                <li
                  key={fact}
                  className="flex items-center gap-2.5 text-body-s text-hur-600"
                >
                  <span
                    aria-hidden="true"
                    className="block size-1.5 shrink-0 rounded-full bg-hur-300"
                  />
                  {fact}
                </li>
              ))}
            </ul>
          </div>

          {/* --- the evidence ------------------------------------------- */}
          <div
            className="rise mx-auto w-full min-w-0 max-w-[460px] lg:col-span-5 lg:mx-0 lg:max-w-none"
            style={delay(3)}
          >
            <RoomPlanCard />
          </div>
        </div>
      </Container>
    </section>
  );
}
