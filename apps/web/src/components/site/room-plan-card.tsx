/**
 * The quiet half of the hero: what a finished scan actually gives you back.
 * Deliberately a plan, not a render, so it does not compete with the 3D
 * section below it. Soft labels rather than dimension arrows, because
 * arrows read as CAD.
 *
 * The plan is capped at its natural 400px. Letting an SVG with text inside
 * stretch to the container makes the labels balloon on tablet and shrink to
 * unreadable on phones, so only secondary facts live outside the drawing
 * where they keep a fixed size.
 */
export function RoomPlanCard() {
  return (
    <div className="min-w-0 rounded-panel border border-hur-200 bg-white p-5 shadow-soft sm:p-7">
      <div className="flex items-center justify-between gap-4">
        <div className="min-w-0">
          <p className="font-display text-[1.0625rem] tracking-[-0.01em] text-hur-900">
            Living room
          </p>
          <p className="text-body-s text-hur-600">
            From a 47 second walkthrough
          </p>
        </div>
        <span className="shrink-0 rounded-full bg-mist-200 px-3 py-1 text-[0.6875rem] font-bold tracking-wider text-mist-800">
          Ready
        </span>
      </div>

      <div className="mt-5 min-w-0 rounded-ctl bg-mist-50 px-3 py-3">
        <svg
          viewBox="14 8 342 226"
          preserveAspectRatio="xMidYMid meet"
          className="mx-auto block h-auto w-full min-w-0 max-w-100"
          role="img"
          aria-label="Floor plan of a living room measuring 4.6 by 3.8 metres. One window on the top wall, a door on the bottom wall, and three pieces of furniture placed against the walls."
        >
          {/* floor */}
          <path
            d="M56 44 H290 V96 H344 V224 H56 Z"
            fill="#eaf1f8"
            stroke="#9bc0de"
            strokeWidth="3"
            strokeLinejoin="round"
          />

          {/* window on the top wall */}
          <line
            x1="150"
            y1="44"
            x2="250"
            y2="44"
            stroke="#f6f9fc"
            strokeWidth="7"
          />
          <line
            x1="150"
            y1="44"
            x2="250"
            y2="44"
            stroke="#3e6b90"
            strokeWidth="3"
          />

          {/* door gap and swing on the bottom wall */}
          <line
            x1="100"
            y1="224"
            x2="170"
            y2="224"
            stroke="#f6f9fc"
            strokeWidth="7"
          />
          <path
            d="M170 224 A70 70 0 0 0 100 154"
            fill="none"
            stroke="#bfd6ea"
            strokeWidth="2"
          />

          {/* placed furniture, kept as soft blocks */}
          <rect x="70" y="86" width="36" height="96" rx="5" fill="#9bc0de" />
          <rect x="132" y="114" width="84" height="50" rx="5" fill="#c4bcb7" />
          <rect x="300" y="132" width="38" height="58" rx="5" fill="#c4bcb7" />

          {/* soft measurements, no arrows */}
          <text
            x="200"
            y="27"
            textAnchor="middle"
            fill="#71655f"
            fontSize="16"
            fontFamily="var(--font-nunito), system-ui, sans-serif"
          >
            4.6 m
          </text>
          <text
            x="32"
            y="134"
            textAnchor="middle"
            fill="#71655f"
            fontSize="16"
            fontFamily="var(--font-nunito), system-ui, sans-serif"
            transform="rotate(-90 32 134)"
          >
            3.8 m
          </text>
        </svg>
      </div>

      <dl className="mt-5 flex flex-wrap gap-x-6 gap-y-2 text-body-s">
        <div className="flex gap-2">
          <dt className="text-hur-600">Ceiling</dt>
          <dd className="text-hur-900 tabular-nums">2.7 m</dd>
        </div>
        <div className="flex gap-2">
          <dt className="text-hur-600">Placed</dt>
          <dd className="text-hur-900 tabular-nums">3 items</dd>
        </div>
      </dl>

      <div className="mt-5 flex items-baseline justify-between gap-4 border-t border-hur-200 pt-5">
        <span className="text-body-s text-hur-600">Budget used</span>
        <span className="font-display text-[1.0625rem] tabular-nums text-hur-900">
          $1,860 <span className="text-hur-600">of $2,400</span>
        </span>
      </div>
      <div
        className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-mist-200"
        role="img"
        aria-label="78 percent of the budget used"
      >
        <div className="h-full w-[78%] rounded-full bg-mist-400" />
      </div>
    </div>
  );
}
