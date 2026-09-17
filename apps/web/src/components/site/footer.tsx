import { Container } from "@/components/ui/container";
import { Wordmark } from "@/components/site/wordmark";

const links = [
  { href: "#how-it-works", label: "How it works" },
  { href: "#features", label: "Features" },
  { href: "#why", label: "Why RoomFittr" },
  { href: "#faq", label: "FAQ" },
];

/**
 * Continues the hur-900 band of the final CTA rather than starting a new one,
 * separated by a hairline. Two dark blocks stacked with a seam between them
 * read as one ending; two separate dark sections read as a mistake.
 */
export function Footer() {
  return (
    <footer className="bg-hur-900 pb-12 text-white">
      <Container>
        <div className="flex flex-col gap-10 border-t border-hur-700 pt-12 md:flex-row md:items-start md:justify-between md:gap-16">
          <div className="max-w-[22rem]">
            <Wordmark tone="light" />
            <p className="mt-4 text-body-s text-mist-200">
              See real furniture in your real room, at a price you set, before
              you buy any of it.
            </p>
          </div>

          <nav aria-label="Footer">
            <ul className="flex flex-wrap gap-x-8 gap-y-3">
              {links.map((link) => (
                <li key={link.href}>
                  <a
                    href={link.href}
                    className="text-body-s text-mist-200 transition-colors duration-200 hover:text-white"
                  >
                    {link.label}
                  </a>
                </li>
              ))}
            </ul>
          </nav>
        </div>

        <p className="mt-12 border-t border-hur-700 pt-8 text-body-s text-mist-400">
          RoomFittr. A personal project, in progress.
        </p>
      </Container>
    </footer>
  );
}
