import type React from "react";

export interface PanelProps {
  /** Grid area name on `.call-grid`: "bar" | "glass" | "wave" | "rail" | "chips". */
  area: string;
  /**
   * "surface" is every instrument panel (bar, wave, rail, chips).
   * "glass" is the inverted teleprompter ground, the darkest region on screen.
   */
  variant?: "surface" | "glass";
  /** MICRO label rendered at the head strip left. */
  kicker?: string;
  /** Head strip right slot. */
  head?: React.ReactNode;
  /**
   * Head strip height. A number is read as px; a string passes straight through so the
   * teleprompter can hand over `var(--tp-head)`. Default is the 32px in the class string.
   */
  headHeight?: number | string;
  className?: string;
  children: React.ReactNode;
}

function headStripHeight(h: number | string): string {
  return typeof h === "number" ? `${h}px` : h;
}

/**
 * The base every instrument sits in. It owns a background, an optional head strip
 * and nothing else. It never owns a border, a radius, a shadow or a margin: the
 * seam grid provides all of those, so the 1px gap between panels IS the hairline.
 *
 * There is no accent edge and no gradient anywhere on it. The depth budget for the
 * whole product is one hairline plus one 5 percent luminance step, and a lit seam on
 * the top edge of a panel spends that budget on decoration instead of on meaning.
 *
 * Panel has no interactive states. If a component needs a hover state on the panel
 * itself, the component is wrong.
 */
export function Panel({
  area,
  variant = "surface",
  kicker,
  head,
  headHeight,
  className,
  children,
}: PanelProps) {
  const hasHead = Boolean(kicker || head);

  return (
    <div
      style={{ gridArea: area }}
      className={`relative flex min-h-0 min-w-0 flex-col ${
        variant === "glass" ? "bg-bg" : "bg-surface"
      }${className ? ` ${className}` : ""}`}
    >
      {hasHead ? (
        <div
          className="flex h-8 shrink-0 items-center justify-between gap-3 border-b border-line px-3"
          style={
            headHeight === undefined
              ? undefined
              : { height: headStripHeight(headHeight) }
          }
        >
          <span className="truncate font-mono text-micro uppercase text-muted">
            {kicker}
          </span>
          {head ? (
            <div className="flex shrink-0 items-center gap-2">{head}</div>
          ) : null}
        </div>
      ) : null}

      <div className="relative flex min-h-0 min-w-0 flex-1 flex-col">
        {children}
      </div>
    </div>
  );
}
