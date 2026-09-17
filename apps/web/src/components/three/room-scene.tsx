"use client";

import type { RefObject } from "react";
import { useState } from "react";
import { Canvas } from "@react-three/fiber";
import { Room } from "@/components/three/room";

/**
 * The three.js entry point, split from RoomCanvas so that the whole renderer
 * sits in its own chunk.
 *
 * `flat` turns off ACES tone mapping. The palette is already a set of pale,
 * carefully measured tokens, and filmic tone mapping desaturates them into
 * something greyer than the design system asks for.
 */
export function RoomScene({
  pointer,
  parallax,
  active,
}: {
  pointer: RefObject<{ x: number; y: number }>;
  parallax: boolean;
  active: boolean;
}) {
  const [ready, setReady] = useState(false);

  return (
    <Canvas
      flat
      shadows="soft"
      dpr={[1, 1.75]}
      frameloop={active ? "always" : "never"}
      camera={{ fov: 30, near: 0.1, far: 60 }}
      gl={{ antialias: true, powerPreference: "high-performance" }}
      onCreated={() => setReady(true)}
      className={`transition-opacity duration-700 ease-calm ${
        ready ? "opacity-100" : "opacity-0"
      }`}
      aria-label="A 3D living room measured from a phone video. Pale blue walls, a window on the back wall, and a highlighted blue sofa alongside a coffee table, a console and a floor lamp."
    >
      <Room pointer={pointer} parallax={parallax} />
    </Canvas>
  );
}
