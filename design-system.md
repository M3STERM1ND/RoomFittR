# RoomFittr Design System (v1)

> The one feeling: **calm and modern.** Every token below serves that. If a choice
> does not, it gets cut. When in doubt: remove, do not add.
>
> Source of truth for product claims: `masterplan.md`, `implementation-plan.md`.
>
> **Naming:** the product is **RoomFittr**. `implementation-plan.md` still says
> "FurnituR" throughout; that document is the older name and should be updated.

---

## 1. Principles

1. **Space is the main material.** Sections breathe at 176px of vertical padding on
   desktop. We add room, not content.
2. **Warm ink, cool air.** Text is a warm near-black (Hurricane family), backgrounds
   are a pale blue (Mist family). That warm/cool split is what keeps it from reading
   clinical or "AI startup".
3. **Two hues, no third.** Mist (pastel blue) dominant, Hurricane (warm grey) accent.
   White and the Hurricane dark end are neutrals, not a third color.
4. **No decoration.** No gradients, no glassmorphism, no floating blobs, no glow.
5. **One idea per section.** If a section is arguing two things, it is two sections
   or it is one section with a cut.
6. **Balance across the full width.** Never strand content in one third.

---

## 2. Color

### Mist (dominant, pastel blue)

| Token | Hex | Use |
|---|---|---|
| `mist-50` | `#F6F9FC` | Page background |
| `mist-100` | `#EAF1F8` | Alternating section wash |
| `mist-200` | `#D8E6F2` | Cards on wash, 3D room wall surfaces |
| `mist-300` | `#BFD6EA` | Hairline borders on Mist surfaces |
| `mist-400` | `#9BC0DE` | The signature pastel blue. Focus ring, 3D accents |
| `mist-600` | `#3E6B90` | Inline link / emphasis text |
| `mist-800` | `#2C4C66` | Deep spatial blue. Shadow tint, 3D key light |

### Hurricane (accent + ink, warm grey)

| Token | Hex | Use |
|---|---|---|
| `hur-100` | `#EFECEA` | Warm surface, 3D floor |
| `hur-200` | `#DDD8D4` | Borders on white |
| `hur-300` | `#C4BCB7` | Dividers on dark, 3D furniture proxies |
| `hur-500` | `#8B7E77` | Hurricane proper. Icons and non-text accents only |
| `hur-600` | `#71655F` | Muted / secondary text |
| `hur-700` | `#5C524D` | Body text |
| `hur-900` | `#2E2926` | Headlines, primary button fill, footer |

Neutrals: `#FFFFFF` for cards and on-dark text.

### Contrast (measured, WCAG 2.1)

| Pair | Ratio | Result |
|---|---|---|
| `hur-900` on `mist-50` | 13.60 | AAA |
| `hur-900` on `mist-200` | 11.31 | AAA |
| `hur-700` on `mist-50` | 7.18 | AAA |
| `hur-700` on `mist-100` | 6.66 | AA |
| `hur-600` on `mist-50` | 5.33 | AA |
| `mist-600` on `mist-50` | 5.36 | AA |
| `#FFFFFF` on `hur-900` | 14.37 | AAA |
| `mist-400` on `hur-900` | 7.52 | AAA |

**Rule:** `hur-500` (#8B7E77) is 3.72 on `mist-50`. It is never used for text.
Icons, rules, and 3D materials only.

---

## 3. Type

Two families, no more.

- **Display:** **Jost** (variable, self-hosted via `next/font/google`). Weights 300,
  400 and 500 only. Never 600+ at display sizes; heavy geometric reads loud, not calm.
  Sentence case always. Never caps, never small-caps.
  *Locked 17 Sep 2026.* Century Gothic was the brief's request but has no free web
  font licence, so roughly half of visitors would have silently fallen back. Jost is
  the same geometric, Futura-descended skeleton and renders identically everywhere.
- **Body:** **Nunito**, weights 400 and 600.

### Scale

Display face: **Jost**. Body face: **Nunito**.

| Token | Desktop | Mobile | Line height | Tracking | Use |
|---|---|---|---|---|---|
| `display-xl` | 76px | 40px | 1.05 | -0.025em | Hero H1 |
| `display-l` | 52px | 32px | 1.12 | -0.02em | Section H2 |
| `display-m` | 30px | 24px | 1.20 | -0.01em | Card H3 |
| `body-l` | 18px | 17px | 1.6 | 0 | Lead paragraph |
| `body-m` | 17px | 16px | 1.6 | 0 | Default body |
| `body-s` | 15px | 15px | 1.55 | 0 | Caption, footer |
| `label` | 14px | 14px | 1.4 | 0.03em | Nav, buttons, eyebrows |

### Measure

- Body paragraphs: `max-width: 65ch`.
- Hero lead: `max-width: 52ch`.
- No paragraph ever runs the full page width.

---

## 4. Space

Base unit 4px. Locked steps only:

`4 · 8 · 12 · 16 · 24 · 32 · 48 · 64 · 96 · 128 · 176 · 240`

| Context | Desktop | Laptop | Tablet | Mobile |
|---|---|---|---|---|
| Section padding (y) | 176px | 128px | 96px | 80px |
| Page gutter (x) | 48px | 40px | 32px | 20px |
| Block gap inside a section | 64px | 48px | 40px | 32px |

- Content max width: **1280px**. Full-bleed bands may run to 1440px.
- Minimum gap between any two sections' content: 96px. Sections never bleed.

---

## 5. Form

**Radii** — two values only.

| Token | Value | Use |
|---|---|---|
| `radius-sm` | 8px | Buttons, inputs, chips |
| `radius-lg` | 20px | Cards, the 3D canvas frame |

**Borders** — 1px hairline. `hur-200` on white, `mist-300` on Mist surfaces.

**Shadows** — two values, cool-tinted, soft. No hard drop shadows.

```
shadow-sm  0 1px 2px  rgba(44,76,102,.04),  0 2px 8px   rgba(44,76,102,.04)
shadow-lg  0 2px 4px  rgba(44,76,102,.03),  0 12px 32px rgba(44,76,102,.07)
```

**Component heights** — control 48px desktop / 44px mobile. Navbar 72px.

**Buttons**

| Variant | Fill | Text | Border | Hover |
|---|---|---|---|---|
| Primary | `hur-900` | `#FFFFFF` | none | rise 1px, `shadow-lg` |
| Secondary | transparent | `hur-900` | 1px `hur-200` | fill `mist-100` |

Focus visible: 2px `mist-400` ring, 2px offset, on every interactive element.

---

## 6. Motion

Three choreographed moments. Nothing else animates.

1. **Hero entrance.** Children fade in and rise 12px. 0.6s, `cubic-bezier(.16,1,.3,1)`,
   70ms stagger. Fires once on load.
2. **Section reveal.** `whileInView` with `once: true`, margin `-80px`. Fade in and
   rise 16px over 0.7s, same easing.
3. **Signature hover.** The 3D room preview: cursor position parallaxes the camera
   by up to ±1.5°, damped. That is the one playful moment on the page.

`prefers-reduced-motion: reduce` disables every transform and the 3D idle motion.
Content appears at full opacity immediately.

---

## 7. The 3D room preview

It supports the product story. It is not decoration.

- **Materials:** walls `mist-200`, floor `hur-100`, furniture proxies `hur-300` with
  one `mist-400` highlight piece. Matte only, no reflections.
- **Light:** soft ambient + one directional key from the window side, tinted
  `mist-800` in shadow. Contact shadows on the floor.
- **Camera:** 35mm perspective, eye level, slight downward tilt. Cursor parallax only.
- **Never:** wireframes, grids, axis gizmos, bounding boxes, neon edges. Those read
  as CAD or as a game.
- **Budget** (matches `implementation-plan.md` §9.2): ≤2k triangles per furniture
  proxy, ≤100k scene triangles, ≤150 draw calls. The 3D bundle is lazy-loaded so
  non-3D content is interactive first.
- **Small screens:** the canvas does not just shrink. It becomes a shorter, wider
  framed view with the camera pulled back and the caption moved below, so the room
  is still legible at 390px.

---

## 8. Responsive intent

Desktop-first. Breakpoints: 1440 / 1280 / 1024 / 768 / 390.

Layout changes are compositional, not just stacked:

- Two-column sections become one column with the visual **first** on mobile only
  where the visual carries the idea, otherwise text first.
- The How It Works row of 3 becomes a vertical timeline on mobile, not 3 stacked cards.
- Navbar collapses to a logo plus a single primary action. No hamburger with one link.
