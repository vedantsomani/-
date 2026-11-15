// Typed display commands: plain words in, a view-state patch out. Deterministic, no network, no AI.
// The patch goes through the same `update()` as every control, so the result is in the shareable URL.
import type { Mode, ViewState } from "./viewState";
import { ALL_DETS, defaultView } from "./viewState";

export interface CommandResult {
  /** Fields of the view to change (empty when nothing was understood). */
  patch: Partial<ViewState>;
  /** Short inline messages: unknown words, bad values, help. */
  messages: string[];
  /** What was applied, in words ("detector 6", "gaia on"), for the confirmation line. */
  applied: string[];
}

const STRETCHES = ["asinh", "linear", "sqrt", "log"] as const;
const ZMAX_RANGE: [number, number] = [3, 20]; // the colour-limit slider
const FOV_RANGE: [number, number] = [1, 30]; // arcmin; the field-of-view menu goes 2-30

const MODE_WORDS: Record<string, Mode> = {
  seasons: "stacks",
  season: "stacks",
  stacks: "stacks",
  stack: "stacks",
  visits: "visits",
  visit: "visits",
  matched: "bins",
  bins: "bins",
  difference: "diff",
  differences: "diff",
  diff: "diff",
  exposures: "frames",
  exposure: "frames",
  frames: "frames",
  frame: "frames",
  colour: "rgb",
  color: "rgb",
  rgb: "rgb",
};
const MODE_NAME: Record<Mode, string> = {
  visits: "visit stacks",
  stacks: "season stacks",
  bins: "matched wavelength bins",
  diff: "differences",
  frames: "exposures",
  rgb: "colour",
};

type ToggleKey = "wise" | "gaia" | "flags" | "marks" | "invert" | "fp";
const TOGGLE_WORDS: Record<string, ToggleKey> = {
  wise: "wise",
  allwise: "wise",
  gaia: "gaia",
  catalogue: "gaia",
  catalog: "gaia",
  catwise: "gaia",
  flags: "flags",
  flag: "flags",
  detections: "marks",
  detection: "marks",
  invert: "invert",
  spectrum: "fp",
};
const TOGGLE_NAME: Record<ToggleKey, string> = {
  wise: "AllWISE sources",
  gaia: "catalogue stars",
  flags: "flagged pixels",
  marks: "detections",
  invert: "invert",
  fp: "spectrum",
};
const ON = new Set(["on", "true", "yes", "show", "open", "1"]);
const OFF = new Set(["off", "false", "no", "hide", "close", "0"]);
const FILLER = new Set([
  "and",
  "then",
  "the",
  "to",
  "set",
  "a",
  "please",
  "show",
]);
const VALUE_WORDS = new Set([
  "fov",
  "zmax",
  "stretch",
  "pair",
  "detector",
  "det",
  "d",
]);

export const COMMAND_HELP =
  "d1-d6 or detector N · reddest · bluest · seasons · visits · matched · difference · exposures · colour · " +
  "gaia|catalogue on|off · allwise on|off · flags on|off · detections on|off · invert on|off · spectrum · fov N · zmax N · " +
  "stretch asinh|linear|sqrt|log · pair 1-2 · reset · help";

const DET_WORD = /^d(?:et(?:ector)?)?(\d+)$/;
const has = (o: object, k: string) =>
  Object.prototype.hasOwnProperty.call(o, k);

const isKeyword = (t: string | undefined) =>
  t !== undefined &&
  (has(MODE_WORDS, t) ||
    has(TOGGLE_WORDS, t) ||
    VALUE_WORDS.has(t) ||
    DET_WORD.test(t) ||
    t === "reset" ||
    t === "help" ||
    t === "reddest" ||
    t === "bluest");

/** Display settings back to their defaults; the target and field of view stay (changing them would reload the field). */
function resetPatch(view: ViewState): Partial<ViewState> {
  const d = defaultView(view.target);
  return {
    section: d.section,
    mode: d.mode,
    det: d.det,
    bin: 0,
    pair: null,
    diffCombined: true,
    zmax: d.zmax,
    dfloor: d.dfloor,
    dgamma: d.dgamma,
    diffLocal: false,
    wise: false,
    gaia: false,
    marks: true,
    flags: false,
    cand: null,
    sel: null,
    dets: ALL_DETS,
    hiddenSeasons: [],
    hideFlagged: true,
    render: d.render,
    invert: false,
    fp: false,
  };
}

/**
 * Parse a line such as "d6 difference gaia on". Case and order do not matter; several commands per line.
 * Commands apply left to right (so "reset d6" resets, then picks D6). Words that are not commands change
 * nothing and are named in `messages` together with what is accepted.
 */
export function parseCommands(text: string, view: ViewState): CommandResult {
  const tokens = text
    .toLowerCase()
    .replace(/[,;=:]+/g, " ")
    .split(/\s+/)
    .filter(Boolean);
  const messages: string[] = [];
  const applied: string[] = [];
  const unknown: string[] = [];
  let patch: Partial<ViewState> = {};
  let det: number | null = null;
  let pairSet = false;
  let wantHelp = false;

  if (!tokens.length)
    return {
      patch,
      applied,
      messages: [
        'Type a command, for example "d6 difference gaia on". Type help for the list.',
      ],
    };

  const bad = (msg: string) => messages.push(msg);

  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    const next = () => tokens[i + 1];
    const take = () => tokens[++i];
    const setDet = (n: number) => {
      det = n;
      applied.push(`detector ${n}`);
    };
    let m: RegExpMatchArray | null;

    if (FILLER.has(t)) continue;
    if (t === "help" || t === "?") {
      wantHelp = true;
      continue;
    }
    if (t === "reset") {
      patch = resetPatch(view);
      det = null;
      pairSet = false;
      applied.push("reset");
      continue;
    }
    if (t === "reddest") {
      setDet(6);
      continue;
    }
    if (t === "bluest") {
      setDet(1);
      continue;
    }
    if ((m = t.match(DET_WORD))) {
      const n = Number(m[1]);
      if (n >= 1 && n <= 6) setDet(n);
      else bad(`"${t}": detectors are 1 to 6.`);
      continue;
    }
    if (t === "detector" || t === "det" || t === "d") {
      const v = isKeyword(next()) ? undefined : next();
      const n = v !== undefined && /^\d+$/.test(v) ? Number(v) : NaN;
      if (v !== undefined) take();
      if (n >= 1 && n <= 6) setDet(n);
      else
        bad(
          v === undefined
            ? `"${t}" needs a detector number, 1 to 6 (for example d6).`
            : `"${v}" is not a detector: use 1 to 6.`,
        );
      continue;
    }
    if (has(MODE_WORDS, t)) {
      patch.mode = MODE_WORDS[t];
      applied.push(`mode: ${MODE_NAME[MODE_WORDS[t]]}`);
      continue;
    }
    if (has(TOGGLE_WORDS, t)) {
      const key = TOGGLE_WORDS[t];
      const v = next();
      let on = true; // a bare "gaia" or "spectrum" turns it on
      if (v !== undefined && ON.has(v)) take();
      else if (v !== undefined && OFF.has(v)) {
        take();
        on = false;
      }
      Object.assign(patch, { [key]: on });
      applied.push(`${TOGGLE_NAME[key]} ${on ? "on" : "off"}`);
      continue;
    }
    if (t === "fov" || t === "zmax") {
      const v = isKeyword(next()) ? undefined : next();
      if (v !== undefined) take();
      const n = v !== undefined && v !== "" ? Number(v) : NaN;
      const [lo, hi] = t === "fov" ? FOV_RANGE : ZMAX_RANGE;
      if (!Number.isFinite(n) || n < lo || n > hi) {
        bad(
          v === undefined
            ? `"${t}" needs a number (for example ${t} ${t === "fov" ? 10 : 8}).`
            : `"${t} ${v}": use a number from ${lo} to ${hi}.`,
        );
      } else {
        patch[t] = n;
        applied.push(`${t} ${n}`);
      }
      continue;
    }
    if (t === "stretch") {
      const v = isKeyword(next()) ? undefined : next();
      if (v !== undefined) take();
      const s = STRETCHES.find((x) => x === v);
      if (!s)
        bad(
          v === undefined
            ? '"stretch" needs asinh, linear, sqrt or log.'
            : `"stretch ${v}": use asinh, linear, sqrt or log.`,
        );
      else {
        patch.render = { ...(patch.render ?? view.render), stretch: s };
        applied.push(`stretch ${s}`);
      }
      continue;
    }
    if (t === "pair") {
      let v = isKeyword(next()) ? undefined : next();
      if (v !== undefined) take();
      if (v !== undefined && /^\d$/.test(v) && /^\d$/.test(next() ?? ""))
        v = `${v}-${take()}`; // "pair 1 2"
      const pm = v?.match(/^(\d)\s*[-–]\s*(\d)$/);
      if (
        v !== undefined &&
        ["auto", "default", "deepest", "none"].includes(v)
      ) {
        patch.pair = null;
        pairSet = true;
        applied.push("pair: deepest");
      } else if (pm && pm[1] !== pm[2]) {
        const a = Math.min(Number(pm[1]), Number(pm[2]));
        const b = Math.max(Number(pm[1]), Number(pm[2]));
        patch.pair = [a, b];
        pairSet = true;
        applied.push(`pair ${a}-${b}`);
      } else {
        bad(
          v === undefined
            ? '"pair" needs two seasons, for example pair 1-2.'
            : `"pair ${v}": use two different season numbers, like 1-2.`,
        );
      }
      continue;
    }
    unknown.push(t);
  }

  // The detector buttons also reset the wavelength bin and the pair; exposures show the chosen detector only.
  if (det !== null) {
    patch.det = det;
    patch.bin = 0;
    if (!pairSet) patch.pair = null;
    if ((patch.mode ?? view.mode) === "frames") patch.dets = [det];
  }
  if (unknown.length) {
    messages.unshift(
      `Not a command: ${unknown.map((u) => `"${u}"`).join(", ")}. Accepted: ${COMMAND_HELP}`,
    );
  }
  if (wantHelp)
    messages.push(`Commands, any order, several per line: ${COMMAND_HELP}`);
  return { patch, messages, applied };
}
