"use client";

import type { RefObject } from "react";
import { useMemo, useRef } from "react";
import { useFrame, useThree } from "@react-three/fiber";
import { Html, PerspectiveCamera, RoundedBox } from "@react-three/drei";
import type { PerspectiveCamera as ThreePerspectiveCamera } from "three";
import { BackSide, MathUtils, Spherical, Vector3 } from "three";

/* --------------------------------------------------------------------------
   Materials come from the design system, not from taste. Walls mist-200,
   floor hur-100, furniture proxies hur-300, one mist-400 highlight piece.
   Matte only: roughness 1, metalness 0, no environment map, no reflections.
   -------------------------------------------------------------------------- */
const WALL = "#d8e6f2";
const CEILING = "#f6f9fc";
const FLOOR = "#efecea";
const RUG = "#ddd8d4";
const PROXY = "#c4bcb7";
const PROXY_DARK = "#b7aea8";
const HIGHLIGHT = "#9bc0de";
const DAYLIGHT = "#ffffff";
const REVEAL = "#bfd6ea";

/* Room in metres, origin at the centre of the floor.
   Generous on purpose: the canvas frame is close to 2:1, and a small room in
   a wide frame means either a fisheye lens or empty space past the walls. */
const W = 6.0;
const D = 4.2;
const H = 2.75;

/* Window on the back wall, which is also where the key light comes from. */
const WIN_X = -0.9;
const WIN_W = 1.7;
const WIN_SILL = 0.8;
const WIN_HEAD = 2.1;

/** The far corner, by the window. Aiming across the room rather than at a
    single object is what gives the shot a floor, a ceiling and two walls. */
const TARGET = new Vector3(-1.4, 0.95, -1.9);

/** Base framing: standing inside the room near the front right corner, at eye
    level, tilted slightly down. Interior photography, not a doll house.
    The camera stays inside the shell at every aspect, which is what makes it
    impossible for the frame to run off the end of the floor. */
const BASE_DISTANCE = 5.39;
const BASE_AZIMUTH = MathUtils.degToRad(47.7);
const BASE_POLAR = MathUtils.degToRad(90 - 7.1);

/** Roughly 28mm on the wide desktop frame, opening up as the frame gets
    shorter. A shorter frame needs a wider lens, not a camera further back:
    further back means outside the room. */
const BASE_FOV = 38;
const BASE_ASPECT = 1.91;
const MAX_FOV = 54;

/** The one playful moment on the page, and its hard ceiling. */
const PARALLAX_DEG = 1.5;

type Pointer = { x: number; y: number };

function Matte({ color }: { color: string }) {
  return <meshStandardMaterial color={color} roughness={1} metalness={0} />;
}

/* -------------------------------------------------------------------------- */

function Shell() {
  const left = -W / 2;
  const winL = WIN_X - WIN_W / 2;
  const winR = WIN_X + WIN_W / 2;
  const pierL = winL - left;
  const pierR = W / 2 - winR;

  return (
    <group>
      {/* floor */}
      <mesh rotation-x={-Math.PI / 2} receiveShadow>
        <planeGeometry args={[W, D]} />
        <Matte color={FLOOR} />
      </mesh>

      {/* ceiling, so the shot reads as a room rather than as a stage */}
      <mesh position-y={H} rotation-x={Math.PI / 2}>
        <planeGeometry args={[W, D]} />
        <Matte color={CEILING} />
      </mesh>

      {/* left wall */}
      <mesh position={[left, H / 2, 0]} rotation-y={Math.PI / 2} receiveShadow>
        <planeGeometry args={[D, H]} />
        <Matte color={WALL} />
      </mesh>

      {/* Right and front walls sit behind the camera and are never seen, but
          they close the box, so no frame edge can ever look out into nothing. */}
      <mesh position={[W / 2, H / 2, 0]} rotation-y={-Math.PI / 2} receiveShadow>
        <planeGeometry args={[D, H]} />
        <Matte color={WALL} />
      </mesh>
      <mesh position={[0, H / 2, D / 2]} rotation-y={Math.PI} receiveShadow>
        <planeGeometry args={[W, H]} />
        <Matte color={WALL} />
      </mesh>

      {/* Back wall, built as four pieces around the window opening. Nothing in
          the shell casts a shadow, which is what lets the key light read as
          daylight through the glass instead of being blocked by the wall. */}
      <group position-z={-D / 2}>
        <mesh position={[left + pierL / 2, H / 2, 0]} receiveShadow>
          <planeGeometry args={[pierL, H]} />
          <Matte color={WALL} />
        </mesh>
        <mesh position={[winR + pierR / 2, H / 2, 0]} receiveShadow>
          <planeGeometry args={[pierR, H]} />
          <Matte color={WALL} />
        </mesh>
        <mesh position={[WIN_X, WIN_SILL / 2, 0]} receiveShadow>
          <planeGeometry args={[WIN_W, WIN_SILL]} />
          <Matte color={WALL} />
        </mesh>
        <mesh position={[WIN_X, (WIN_HEAD + H) / 2, 0]} receiveShadow>
          <planeGeometry args={[WIN_W, H - WIN_HEAD]} />
          <Matte color={WALL} />
        </mesh>

        {/* reveal, then the glass, held flat so it reads as blown out */}
        <mesh position={[WIN_X, (WIN_SILL + WIN_HEAD) / 2, -0.03]}>
          <planeGeometry args={[WIN_W, WIN_HEAD - WIN_SILL]} />
          <meshBasicMaterial color={REVEAL} />
        </mesh>
        <mesh position={[WIN_X, (WIN_SILL + WIN_HEAD) / 2, -0.05]}>
          <planeGeometry args={[WIN_W - 0.16, WIN_HEAD - WIN_SILL - 0.16]} />
          <meshBasicMaterial color={DAYLIGHT} />
        </mesh>
        <mesh position={[WIN_X, (WIN_SILL + WIN_HEAD) / 2, -0.04]}>
          <planeGeometry args={[0.045, WIN_HEAD - WIN_SILL - 0.16]} />
          <meshBasicMaterial color={REVEAL} />
        </mesh>
      </group>
    </group>
  );
}

/** The highlighted piece. One mist-400 object in the room, no more. */
function Sofa() {
  return (
    <group position={[-2.42, 0, -0.45]}>
      <RoundedBox
        args={[0.95, 0.34, 2.2]}
        radius={0.05}
        position-y={0.17}
        castShadow
        receiveShadow
      >
        <Matte color={HIGHLIGHT} />
      </RoundedBox>
      <RoundedBox
        args={[0.82, 0.17, 2.04]}
        radius={0.06}
        position={[0.02, 0.42, 0]}
        castShadow
        receiveShadow
      >
        <Matte color={HIGHLIGHT} />
      </RoundedBox>
      <RoundedBox
        args={[0.25, 0.56, 2.2]}
        radius={0.06}
        position={[-0.35, 0.58, 0]}
        castShadow
        receiveShadow
      >
        <Matte color={HIGHLIGHT} />
      </RoundedBox>
      {[-0.99, 0.99].map((z) => (
        <RoundedBox
          key={z}
          args={[0.88, 0.3, 0.22]}
          radius={0.06}
          position={[-0.01, 0.47, z]}
          castShadow
          receiveShadow
        >
          <Matte color={HIGHLIGHT} />
        </RoundedBox>
      ))}
    </group>
  );
}

const TABLE_LEGS: [number, number][] = [
  [-0.26, -0.5],
  [0.26, -0.5],
  [-0.26, 0.5],
  [0.26, 0.5],
];

function CoffeeTable() {
  return (
    <group position={[-1.15, 0, -0.15]}>
      <RoundedBox
        args={[0.68, 0.05, 1.2]}
        radius={0.02}
        position-y={0.4}
        castShadow
        receiveShadow
      >
        <Matte color={PROXY} />
      </RoundedBox>
      {TABLE_LEGS.map(([x, z]) => (
        <mesh key={`${x}:${z}`} position={[x, 0.19, z]} castShadow>
          <boxGeometry args={[0.05, 0.38, 0.05]} />
          <Matte color={PROXY_DARK} />
        </mesh>
      ))}
    </group>
  );
}

/** Balances the right half of the frame, which is otherwise bare floor. */
function Armchair() {
  return (
    <group position={[0.55, 0, -1.15]} rotation-y={-Math.PI / 2 + 0.35}>
      <RoundedBox
        args={[0.88, 0.34, 0.9]}
        radius={0.05}
        position-y={0.17}
        castShadow
        receiveShadow
      >
        <Matte color={PROXY} />
      </RoundedBox>
      <RoundedBox
        args={[0.76, 0.16, 0.76]}
        radius={0.06}
        position={[0.02, 0.42, 0]}
        castShadow
        receiveShadow
      >
        <Matte color={PROXY} />
      </RoundedBox>
      <RoundedBox
        args={[0.24, 0.52, 0.9]}
        radius={0.06}
        position={[-0.32, 0.56, 0]}
        castShadow
        receiveShadow
      >
        <Matte color={PROXY} />
      </RoundedBox>
    </group>
  );
}

function Console() {
  return (
    <RoundedBox
      args={[2.2, 0.45, 0.42]}
      radius={0.03}
      position={[WIN_X, 0.225, -D / 2 + 0.24]}
      castShadow
      receiveShadow
    >
      <Matte color={PROXY} />
    </RoundedBox>
  );
}

function FloorLamp() {
  return (
    <group position={[-2.55, 0, -1.72]}>
      <mesh position-y={0.015} castShadow>
        <cylinderGeometry args={[0.17, 0.17, 0.03, 24]} />
        <Matte color={PROXY_DARK} />
      </mesh>
      <mesh position-y={0.82} castShadow>
        <cylinderGeometry args={[0.018, 0.018, 1.58, 12]} />
        <Matte color={PROXY_DARK} />
      </mesh>
      <mesh position-y={1.72} castShadow>
        <cylinderGeometry args={[0.16, 0.21, 0.3, 24, 1, true]} />
        <meshStandardMaterial
          color={FLOOR}
          roughness={1}
          metalness={0}
          side={BackSide}
        />
      </mesh>
    </group>
  );
}

function Rug() {
  return (
    <mesh position={[-1.3, 0.004, 0.05]} rotation-x={-Math.PI / 2} receiveShadow>
      <planeGeometry args={[3.0, 3.2]} />
      <Matte color={RUG} />
    </mesh>
  );
}

/* -------------------------------------------------------------------------- */

/**
 * Motion moment three. The cursor nudges the camera by at most 1.5 degrees on
 * each axis, damped so it trails the pointer instead of snapping to it.
 *
 * The lens widens and the camera steps back as the frame gets shorter, which
 * is how the composition survives a 280px tall canvas on a phone instead of
 * just shrinking.
 */
function Camera({
  pointer,
  parallax,
}: {
  pointer: RefObject<Pointer>;
  parallax: boolean;
}) {
  const aspect = useThree((s) => s.viewport.aspect);
  const cam = useRef<ThreePerspectiveCamera>(null);
  const angle = useRef({ az: BASE_AZIMUTH, pol: BASE_POLAR });
  const spherical = useMemo(() => new Spherical(), []);

  const fov = MathUtils.clamp(
    2 *
      MathUtils.radToDeg(
        Math.atan(
          Math.tan(MathUtils.degToRad(BASE_FOV / 2)) * (BASE_ASPECT / aspect),
        ),
      ),
    BASE_FOV,
    MAX_FOV,
  );

  useFrame((_, dt) => {
    if (!cam.current) return;
    const p = parallax ? pointer.current : { x: 0, y: 0 };
    const targetAz = BASE_AZIMUTH + MathUtils.degToRad(PARALLAX_DEG) * p.x;
    const targetPol = BASE_POLAR - MathUtils.degToRad(PARALLAX_DEG) * p.y;

    angle.current.az = MathUtils.damp(angle.current.az, targetAz, 4, dt);
    angle.current.pol = MathUtils.damp(angle.current.pol, targetPol, 4, dt);

    spherical.set(BASE_DISTANCE, angle.current.pol, angle.current.az);
    cam.current.position.setFromSpherical(spherical).add(TARGET);
    cam.current.lookAt(TARGET);
  });

  return (
    <PerspectiveCamera ref={cam} makeDefault fov={fov} near={0.1} far={60} />
  );
}

export function Room({
  pointer,
  parallax,
}: {
  pointer: RefObject<Pointer>;
  parallax: boolean;
}) {
  return (
    <>
      <Camera pointer={pointer} parallax={parallax} />

      {/* Three.js divides diffuse by PI, so an ambient intensity near PI is
          what makes a matte surface render close to the colour it was authored
          with. Well below that and every token drifts to a darker, greyer
          version of itself, which is what made the first pass read as slate
          rather than mist.

          The fill is all but neutral rather than blue. A blue ambient strong
          enough to tint the shadows also cancels the warmth out of the hur-100
          floor, and warm floor against cool walls is the whole "warm ink, cool
          air" idea. The hues come from the tokens themselves; the lights just
          expose them. */}
      <ambientLight color="#f7fbff" intensity={2.65} />
      {/* One key, raking in from the window side, barely warm. The wall
          holding the window is the one wall it cannot light, which is true of
          real rooms too. */}
      <directionalLight
        color="#fff6e8"
        position={[1.2, 3.6, -4.2]}
        target-position={[-1.4, 0.2, 1.4]}
        intensity={1.75}
        castShadow
        shadow-mapSize={[1024, 1024]}
        shadow-bias={-0.0006}
        shadow-normalBias={0.03}
        shadow-camera-near={0.5}
        shadow-camera-far={18}
        shadow-camera-left={-6}
        shadow-camera-right={6}
        shadow-camera-top={6}
        shadow-camera-bottom={-6}
      />

      <Shell />
      <Rug />
      <Sofa />
      <CoffeeTable />
      <Armchair />
      <Console />
      <FloorLamp />

      {/* The product story, pinned to the highlighted piece: everything in the
          room is a real item at a real price, not a generic grey block. */}
      <Html
        position={[-2.25, 1.18, 0.2]}
        center
        pointerEvents="none"
        zIndexRange={[10, 0]}
      >
        <div className="flex w-max items-center gap-2.5 rounded-ctl border border-hur-200 bg-white px-3 py-2 shadow-lift">
          <span
            aria-hidden="true"
            className="block size-2 shrink-0 rounded-full bg-mist-400"
          />
          <span className="font-body text-[0.8125rem] font-semibold whitespace-nowrap text-hur-900">
            Linen 3 seater
          </span>
          <span className="font-body text-[0.8125rem] tabular-nums whitespace-nowrap text-hur-600">
            $890
          </span>
        </div>
      </Html>
    </>
  );
}
