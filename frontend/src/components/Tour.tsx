import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { useReducedMotion } from "../hooks/useReducedMotion";
import { markTourSeen } from "../lib/tourStorage";
import type { ViewState } from "../lib/viewState";
import { IconClose } from "./icons";

type Placement = "left" | "right" | "above" | "below";

interface TourAction {
  label: string;
  patch: Partial<ViewState>;
}

interface TourStep {
  /** `data-tour` value of the element to point at. */
  anchor: string;
  title: string;
  body: ReactNode;
  actions?: TourAction[];
  /** Shown instead of the pointer when the element is not on screen (yet). */
  missing?: string;
  /** Where the card may sit relative to the element, in order of preference. */
  prefer: Placement[];
}

const STEPS: TourStep[] = [
  {
    anchor: "image",
    title: "The sky, seen by SPHEREx",
    body: (
      <>
        A 5′ patch of sky around WISE 0855−0714, the coldest known brown
        dwarf, at 4.4–5.0 µm. The <b>ring</b> marks where the object should be
        on this image’s date, from its known motion; the <b>cross</b> is the
        field centre. Click any source to inspect it.
      </>
    ),
    prefer: ["right", "below", "above"],
  },
  {
    anchor: "player",
    title: "Blink the images",
    body: (
      <>
        <b>Play</b> (or Space) flips through images taken months apart:
        anything fixed stays put, anything that moves jumps. The arrow keys
        step one image; the timeline below shows when each image was taken.
      </>
    ),
    prefer: ["right", "below", "above"],
  },
  {
    anchor: "modes",
    title: "Choose what to compare",
    body: (
      <>
        <b>Seasons</b> combines each observing season into one deeper image.{" "}
        <b>λ-matched</b> compares at one exact infrared colour.{" "}
        <b>Difference</b> subtracts one season from another, so a mover shows as
        a bright dot beside a dark one. <b>Exposures</b> shows every raw frame.
      </>
    ),
    actions: [
      { label: "Show Difference", patch: { mode: "diff" } },
      { label: "Back to Seasons", patch: { mode: "stacks" } },
    ],
    prefer: ["left", "below", "above"],
  },
  {
    anchor: "detectors",
    title: "Pick a detector (a colour band)",
    body: (
      <>
        SPHEREx has six detectors, each sensitive to a different infrared
        colour. WISE 0855 is very cold, so it only glows at 4–5 µm. Try{" "}
        <b>D2</b>: it is invisible. Then <b>D6</b>: there it is, drifting
        between seasons.
      </>
    ),
    actions: [
      {
        label: "Show D2",
        patch: { mode: "stacks", det: 2, bin: 0, pair: null },
      },
      {
        label: "Show D6",
        patch: { mode: "stacks", det: 6, bin: 0, pair: null },
      },
    ],
    prefer: ["left", "below", "above"],
  },
  {
    anchor: "verdict",
    title: "The verdict and its evidence",
    body: (
      <>
        An automatic check of whether the object really moved: seen in more
        than one detector, in independent season comparisons, with the same
        speed and direction each time. Each tick is one check that passed.
      </>
    ),
    missing:
      "The verdict panel appears here once the images have been analysed.",
    prefer: ["left", "below", "above"],
  },
  {
    anchor: "spectrum",
    title: "Spectrum and light curve",
    body: (
      <>
        The <b>Spectrum</b> tab measures the object’s brightness
        at every wavelength, with neighbouring stars fitted out. Cold brown
        dwarfs shine only at the red end, which is how you can tell what kind of
        object you found. <b>Prediction</b> then tests the motion on a season
        it has not seen. Press <b>/</b> or <b>Ctrl K</b> for every command.
      </>
    ),
    prefer: ["left", "below", "above"],
  },
];

interface Rect {
  left: number;
  top: number;
  width: number;
  height: number;
}

const CARD_W = 352;
const MARGIN = 12;

const clamp = (v: number, lo: number, hi: number) =>
  Math.min(Math.max(v, lo), Math.max(lo, hi));

/** Position of the card next to `r`, or null when no side has room. */
function place(
  r: Rect,
  h: number,
  vw: number,
  vh: number,
  prefer: Placement[],
): { left: number; top: number } | null {
  // Only the part of the element that is on screen matters.
  const top0 = Math.max(r.top, 0);
  const bottom0 = Math.min(r.top + r.height, vh);
  for (const p of prefer) {
    if (p === "below") {
      const top = bottom0 + MARGIN;
      if (top + h <= vh - 8)
        return { left: clamp(r.left, 8, vw - CARD_W - 8), top };
    } else if (p === "above") {
      const top = top0 - MARGIN - h;
      if (top >= 8) return { left: clamp(r.left, 8, vw - CARD_W - 8), top };
    } else if (p === "right") {
      const left = r.left + r.width + MARGIN;
      if (left + CARD_W <= vw - 8)
        return { left, top: clamp(top0, 8, vh - h - 8) };
    } else {
      const left = r.left - MARGIN - CARD_W;
      if (left >= 8) return { left, top: clamp(top0, 8, vh - h - 8) };
    }
  }
  return null;
}

/** Live bounding box of `[data-tour=anchor]`, or null while it is not rendered. */
function useAnchorRect(anchor: string): Rect | null {
  const [state, setState] = useState<{ anchor: string; rect: Rect | null }>({
    anchor,
    rect: null,
  });
  useEffect(() => {
    let last = "";
    let raf = 0;
    const measure = () => {
      const el = document.querySelector<HTMLElement>(`[data-tour="${anchor}"]`);
      const b =
        el && el.getClientRects().length > 0
          ? el.getBoundingClientRect()
          : null;
      const key = b
        ? `${Math.round(b.left)},${Math.round(b.top)},${Math.round(b.width)},${Math.round(b.height)}`
        : "";
      if (key === last) return;
      last = key;
      setState({
        anchor,
        rect: b
          ? { left: b.left, top: b.top, width: b.width, height: b.height }
          : null,
      });
    };
    const schedule = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(measure);
    };
    schedule();
    const timer = window.setInterval(measure, 250); // layout also changes without scroll or resize events
    window.addEventListener("scroll", schedule, true);
    window.addEventListener("resize", schedule);
    return () => {
      cancelAnimationFrame(raf);
      window.clearInterval(timer);
      window.removeEventListener("scroll", schedule, true);
      window.removeEventListener("resize", schedule);
    };
  }, [anchor]);
  return state.anchor === anchor ? state.rect : null;
}

interface Props {
  /** Closes the tour; `finished` is true when the last step was completed. */
  onClose: (finished: boolean) => void;
  /** Changes the app's view (the demo buttons on some steps). */
  onPatch: (patch: Partial<ViewState>) => void;
  onSection?: (anchor: string) => void;
}

/**
 * Hand-written, non-modal guided tour. It never blocks the page (the dimmed surround ignores the
 * pointer), closes with Esc, and moves with the arrow keys or Enter while focus is in the card.
 */
export function Tour({ onClose, onPatch, onSection }: Props) {
  const [i, setI] = useState(0);
  const step = STEPS[i];
  useEffect(() => {
    onSection?.(step.anchor);
  }, [step.anchor, onSection]);
  const last = i === STEPS.length - 1;
  const rect = useAnchorRect(step.anchor);
  const reduced = useReducedMotion();
  const titleId = useId();
  const bodyId = useId();
  const cardRef = useRef<HTMLDivElement>(null);
  const nextRef = useRef<HTMLButtonElement>(null);
  const opener = useRef<Element | null>(null);
  const [cardH, setCardH] = useState(230);
  const [vp, setVp] = useState({ w: window.innerWidth, h: window.innerHeight });

  const close = useCallback(
    (finished: boolean) => {
      markTourSeen();
      onClose(finished);
    },
    [onClose],
  );

  // Focus the card's main button on open; give focus back when the tour ends.
  useEffect(() => {
    opener.current = document.activeElement;
    nextRef.current?.focus({ preventScroll: true });
    return () => {
      const el = opener.current;
      if (el instanceof HTMLElement && document.contains(el))
        el.focus({ preventScroll: true });
    };
  }, []);

  // Esc closes from anywhere on the page.
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") close(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [close]);

  // Card and viewport size, for placement.
  useLayoutEffect(() => {
    const el = cardRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setCardH(el.offsetHeight));
    ro.observe(el);
    const onResize = () =>
      setVp({ w: window.innerWidth, h: window.innerHeight });
    window.addEventListener("resize", onResize);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", onResize);
    };
  }, []);

  const narrow = vp.w < 640;
  const found = rect !== null;

  // Bring the element into view once per step (a bottom sheet on phones covers the lower part of the screen).
  useEffect(() => {
    if (!found) return;
    const el = document.querySelector<HTMLElement>(
      `[data-tour="${step.anchor}"]`,
    );
    el?.scrollIntoView({
      block: narrow ? "start" : "center",
      inline: "nearest",
      behavior: reduced ? "auto" : "smooth",
    });
  }, [step.anchor, found, narrow, reduced]);

  const go = (d: number) =>
    setI((n) => Math.min(Math.max(n + d, 0), STEPS.length - 1));

  const onCardKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "ArrowRight" || e.key === "ArrowDown") {
      e.preventDefault();
      e.stopPropagation(); // keep the blink player's own arrow keys out of it
      go(1);
    } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
      e.preventDefault();
      e.stopPropagation();
      go(-1);
    } else if (e.key === "Enter" && e.target === e.currentTarget) {
      e.preventDefault();
      if (last) close(true);
      else go(1);
    }
  };

  let style: CSSProperties = {};
  if (!narrow) {
    const side = rect
      ? place(
          rect,
          cardH,
          vp.w,
          vp.h,
          vp.w < 1024 ? ["below", "above"] : step.prefer,
        )
      : null;
    style = side ?? {
      left: Math.max(8, vp.w - CARD_W - 16),
      top: Math.max(8, vp.h - cardH - 16),
    };
  }

  const pad = 6;
  return (
    <>
      {rect && (
        <div
          aria-hidden="true"
          className="pointer-events-none fixed z-[70] rounded-lg transition-all duration-150"
          style={{
            left: rect.left - pad,
            top: rect.top - pad,
            width: rect.width + 2 * pad,
            height: rect.height + 2 * pad,
            boxShadow: "0 0 0 2px #5c9cff, 0 0 0 9999px rgba(5, 6, 8, 0.55)",
          }}
        />
      )}
      <div
        ref={cardRef}
        role="dialog"
        aria-modal="false"
        aria-labelledby={titleId}
        aria-describedby={bodyId}
        tabIndex={-1}
        onKeyDown={onCardKey}
        style={style}
        className={`fixed z-[71] flex flex-col gap-3 rounded-xl border border-line bg-ink-900 p-4 shadow-[0_16px_48px_rgb(0_0_0/0.6)] ${
          narrow ? "inset-x-2 bottom-2 max-h-[55vh] overflow-y-auto" : ""
        }`}
      >
        <div
          style={narrow ? undefined : { width: CARD_W - 32 }}
          className="flex flex-col gap-3"
        >
          <div className="flex items-start justify-between gap-3">
            <div className="flex flex-col gap-0.5">
              <p className="text-xs font-medium text-ink-400 tabular">
                Guided tour · step {i + 1} of {STEPS.length}
              </p>
              <h2 id={titleId} className="text-base font-semibold text-white">
                {step.title}
              </h2>
            </div>
            <button
              type="button"
              className="btn btn-quiet btn-sm -mt-1 -mr-1.5 shrink-0"
              onClick={() => close(false)}
              aria-label="Close the tour"
            >
              <IconClose />
            </button>
          </div>

          <div
            id={bodyId}
            aria-live="polite"
            className="text-sm leading-relaxed text-ink-300"
          >
            {step.body}
            {!found && step.missing && (
              <p className="mt-2 text-xs text-ink-500">{step.missing}</p>
            )}
          </div>

          {step.actions && (
            <div className="flex flex-wrap gap-2">
              {step.actions.map((a) => (
                <button
                  key={a.label}
                  type="button"
                  className="btn btn-sm"
                  onClick={() => onPatch(a.patch)}
                >
                  {a.label}
                </button>
              ))}
            </div>
          )}

          <div className="flex items-center justify-between gap-2">
            <div className="flex gap-1.5" aria-hidden="true">
              {STEPS.map((s, n) => (
                <span
                  key={s.anchor}
                  className={`h-1.5 w-1.5 rounded-full ${n === i ? "bg-accent" : n < i ? "bg-ink-500" : "bg-ink-700"}`}
                />
              ))}
            </div>
            <div className="flex gap-2">
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => go(-1)}
                disabled={i === 0}
              >
                Back
              </button>
              <button
                ref={nextRef}
                type="button"
                className="btn btn-sm btn-primary"
                onClick={() => (last ? close(true) : go(1))}
              >
                {last ? "Finish" : "Next"}
              </button>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
