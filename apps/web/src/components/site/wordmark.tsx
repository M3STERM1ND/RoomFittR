export function Wordmark({
  className = "",
  tone = "ink",
}: {
  className?: string;
  /** "light" is for the footer, which runs on hur-900. */
  tone?: "ink" | "light";
}) {
  return (
    <span className={`inline-flex items-center gap-2.5 ${className}`}>
      <span
        aria-hidden="true"
        className="block size-3 shrink-0 rounded-full bg-mist-400"
      />
      <span
        className={`font-display text-[1.1875rem] tracking-[-0.01em] ${
          tone === "light" ? "text-white" : "text-hur-900"
        }`}
      >
        RoomFittr
      </span>
    </span>
  );
}
