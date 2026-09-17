"use client";

import { useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";

/**
 * Everything three.js related lives behind this import. It is client only and
 * it is not fetched until the frame is within a viewport of the scroll
 * position, which keeps the initial JS for the page under the 200 kB budget in
 * implementation-plan.md section 9.2.
 */
const Scene = dynamic(
  () => import("@/components/three/room-scene").then((m) => m.RoomScene),
  { ssr: false },
);

export function RoomCanvas() {
  const frame = useRef<HTMLDivElement>(null);
  const pointer = useRef({ x: 0, y: 0 });
  const [load, setLoad] = useState(false);
  const [visible, setVisible] = useState(false);
  const [parallax, setParallax] = useState(true);

  useEffect(() => {
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
    const apply = () => setParallax(!reduce.matches);
    apply();
    reduce.addEventListener("change", apply);
    return () => reduce.removeEventListener("change", apply);
  }, []);

  useEffect(() => {
    const el = frame.current;
    if (!el) return;

    // Two thresholds from one observer: a generous margin decides when to
    // fetch the bundle, and actual intersection decides whether to spend
    // frames rendering. Scrolled past, the scene stops drawing entirely.
    const near = new IntersectionObserver(
      ([e]) => {
        if (e.isIntersecting) {
          setLoad(true);
          near.disconnect();
        }
      },
      { rootMargin: "400px 0px" },
    );
    const onscreen = new IntersectionObserver(
      ([e]) => setVisible(e.isIntersecting),
      { rootMargin: "80px 0px" },
    );

    near.observe(el);
    onscreen.observe(el);
    return () => {
      near.disconnect();
      onscreen.disconnect();
    };
  }, []);

  const track = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!parallax) return;
    const r = e.currentTarget.getBoundingClientRect();
    pointer.current = {
      x: ((e.clientX - r.left) / r.width) * 2 - 1,
      y: ((e.clientY - r.top) / r.height) * 2 - 1,
    };
  };

  return (
    <div
      ref={frame}
      onPointerMove={track}
      onPointerLeave={() => (pointer.current = { x: 0, y: 0 })}
      className="relative h-72 w-full overflow-hidden rounded-panel border border-mist-300 bg-mist-200 shadow-soft sm:h-96 md:h-120 lg:h-140 xl:h-155"
    >
      {load ? (
        <Scene pointer={pointer} parallax={parallax} active={visible} />
      ) : null}
    </div>
  );
}
