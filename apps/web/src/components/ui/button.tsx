import type { ComponentPropsWithoutRef, ReactNode } from "react";

type Variant = "primary" | "secondary" | "inverse";

const base =
  "inline-flex items-center justify-center rounded-ctl font-body font-semibold " +
  "text-[0.9375rem] tracking-[0.01em] h-11 md:h-12 px-6 border " +
  "transition-[transform,box-shadow,background-color] duration-200 ease-calm " +
  "motion-reduce:transition-none select-none";

const variants: Record<Variant, string> = {
  // The one signature hover on the page: a 1px lift, nothing more.
  primary:
    "bg-hur-900 text-white border-transparent hover:-translate-y-px hover:shadow-lift",
  secondary:
    "bg-transparent text-hur-900 border-hur-200 hover:bg-mist-100",
  // Primary inverted, for the one band that runs on hur-900. Same lift; the
  // shadow is dropped because a cool shadow does nothing on a dark ground.
  inverse:
    "bg-white text-hur-900 border-transparent hover:-translate-y-px",
};

type Props = {
  variant?: Variant;
  children: ReactNode;
  className?: string;
};

export function Button({
  variant = "primary",
  children,
  className = "",
  ...rest
}: Props & ComponentPropsWithoutRef<"button">) {
  return (
    <button className={`${base} ${variants[variant]} ${className}`} {...rest}>
      {children}
    </button>
  );
}

export function ButtonLink({
  variant = "primary",
  children,
  className = "",
  ...rest
}: Props & ComponentPropsWithoutRef<"a">) {
  return (
    <a className={`${base} ${variants[variant]} ${className}`} {...rest}>
      {children}
    </a>
  );
}
