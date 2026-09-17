"use client";

import { useEffect, useState } from "react";
import { Container } from "@/components/ui/container";
import { ButtonLink } from "@/components/ui/button";
import { Wordmark } from "@/components/site/wordmark";

const links = [
  { href: "#how-it-works", label: "How it works" },
  { href: "#features", label: "Features" },
  { href: "#why", label: "Why RoomFittr" },
  { href: "#faq", label: "FAQ" },
];

export function Navbar() {
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <header
      className={`sticky top-0 z-50 border-b transition-colors duration-300 ease-calm ${
        scrolled
          ? "border-mist-300 bg-mist-50/95 backdrop-blur-[2px]"
          : "border-transparent bg-transparent"
      }`}
    >
      <Container>
        <nav
          aria-label="Main"
          className="flex h-18 items-center justify-between gap-8"
        >
          <a
            href="#top"
            className="rounded-xs focus-visible:outline-2"
            aria-label="RoomFittr, back to top"
          >
            <Wordmark />
          </a>

          {/* Links sit with the action rather than floating mid-row, which
              otherwise leaves a dead gap on wide screens. */}
          <div className="flex items-center gap-9 lg:gap-10">
            <ul className="hidden items-center gap-9 lg:flex">
              {links.map((link) => (
                <li key={link.href}>
                  <a
                    href={link.href}
                    className="text-label font-body font-semibold text-hur-700 transition-colors duration-200 hover:text-hur-900"
                  >
                    {link.label}
                  </a>
                </li>
              ))}
            </ul>

            <ButtonLink href="#scan" className="shrink-0">
              Scan a room
            </ButtonLink>
          </div>
        </nav>
      </Container>
    </header>
  );
}
