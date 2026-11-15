// Every view is shareable: the complete view state round-trips through the URL.
// Defaults are omitted to keep links short.
import { DEFAULT_RENDER, type RenderOpts, type Scale } from "./api";
import {
  SPHEREX_REF_MJD,
  parseDec,
  parseRa,
  type ProperMotion,
} from "./coords";

export interface Target {
  ra: number;
  dec: number;
  label: string;
  /** Set for resolved objects with significant proper motion; ra/dec are then the
   *  position at pm.epochMjd (mid-survey), not the catalogue J2000 position. */
  pm?: ProperMotion;
}

export type Mode = "stacks" | "visits" | "bins" | "diff" | "frames" | "rgb";
export const MODES: Mode[] = [
  "stacks",
  "visits",
  "bins",
  "diff",
  "frames",
  "rgb",
];

export type NotebookSection =
  "observe" | "evidence" | "spectrum" | "prediction" | "sky";
export const NOTEBOOK_SECTIONS: NotebookSection[] = [
  "observe",
  "evidence",
  "spectrum",
  "prediction",
  "sky",
];

export interface ViewState {
  section: NotebookSection;
  target: Target | null;
  fov: number;
  mode: Mode;
  /** Detector blinked in stack mode (stacks are only comparable within one detector). */
  det: number;
  /** Wavelength bin index within the detector (bins mode; diff mode when not combined). */
  bin: number;
  /** Season pair for differences, e.g. [1, 2]; null = the deepest available pair. */
  pair: [number, number] | null;
  /** Difference image: combined over all wavelength bins of the detector, or one bin. */
  diffCombined: boolean;
  zmax: number;
  /** Difference palette: colour starts at this |z| (sigma). */
  dfloor: number;
  /** Difference palette contrast exponent (0.4 = soft, 2 = harsh). */
  dgamma: number;
  /** Difference palette scaled by the LOCAL noise (honest near saturated stars). */
  diffLocal: boolean;
  /** Draw Gaia catalogue stars (and their motions) on the image. */
  gaia: boolean;
  /** Draw AllWISE catalogue sources (and their motions) on the image. */
  wise: boolean;
  /** Draw detection markers on the image. */
  marks: boolean;
  /** Overlay flagged pixels (exposures) / pixels without usable data (stacks). */
  flags: boolean;
  /** Highlighted candidate id. */
  cand: string | null;
  /** Source picked by clicking the image (sky position; the investigate card is shown for it). */
  sel: { ra: number; dec: number } | null;
  /** Detectors shown in exposure mode. */
  dets: number[];
  hiddenSeasons: number[];
  hideFlagged: boolean;
  render: RenderOpts;
  invert: boolean;
  /** Spectrum & light curve (source fingerprint) section is open. */
  fp: boolean;
}

export const ALL_DETS = [1, 2, 3, 4, 5, 6];

/** Brown-dwarf-like movers are brightest at 4–5 µm; most other targets peak around 2 µm. */
export const defaultDetector = (t: Target | null) => (t?.pm ? 6 : 3);

export function defaultView(target: Target | null = null): ViewState {
  return {
    target,
    section: "observe",
    fov: 5,
    mode: "stacks",
    det: defaultDetector(target),
    bin: 0,
    pair: null,
    diffCombined: true,
    zmax: 6,
    dfloor: 1.5,
    dgamma: 0.8,
    diffLocal: false,
    gaia: false,
    wise: false,
    marks: true,
    flags: false,
    cand: null,
    sel: null,
    dets: ALL_DETS,
    hiddenSeasons: [],
    hideFlagged: true,
    render: DEFAULT_RENDER,
    invert: false,
    fp: false,
  };
}

const num = (v: string | null) =>
  v !== null && v !== "" && Number.isFinite(Number(v)) ? Number(v) : null;
const digits = (v: string | null) =>
  v ? [...v].map(Number).filter((d) => d >= 0 && d <= 9) : null;

export function readView(search = window.location.search): ViewState {
  const p = new URLSearchParams(search);
  const ra = p.get("ra") ? parseRa(p.get("ra")!) : null;
  const dec = p.get("dec") ? parseDec(p.get("dec")!) : null;
  let target: Target | null =
    ra !== null && dec !== null
      ? { ra, dec, label: p.get("name") ?? "" }
      : null;
  const pmra = num(p.get("pmra"));
  const pmdec = num(p.get("pmdec"));
  if (target && pmra !== null && pmdec !== null) {
    target = {
      ...target,
      pm: { pmra, pmdec, epochMjd: num(p.get("pmepoch")) ?? SPHEREX_REF_MJD },
    };
  }
  const v = defaultView(target);
  const fov = num(p.get("fov"));
  if (fov && fov > 0) v.fov = fov;
  const mode = p.get("mode") as Mode | null;
  if (mode && MODES.includes(mode)) v.mode = mode;
  const bin = num(p.get("bin"));
  if (bin !== null && bin >= 0) v.bin = bin;
  const pair = p.get("pair")?.match(/^(\d)-(\d)$/);
  if (pair) v.pair = [Number(pair[1]), Number(pair[2])];
  if (p.get("dbin") === "1") v.diffCombined = false;
  const zmax = num(p.get("zmax"));
  if (zmax && zmax > 0) v.zmax = zmax;
  const dfloor = num(p.get("dfloor"));
  if (dfloor !== null && dfloor >= 0 && dfloor <= 5) v.dfloor = dfloor;
  const dgamma = num(p.get("dgamma"));
  if (dgamma !== null && dgamma >= 0.3 && dgamma <= 2.5) v.dgamma = dgamma;
  if (p.get("dlocal") === "1") v.diffLocal = true;
  if (p.get("gaia") === "1") v.gaia = true;
  if (p.get("wise") === "1") v.wise = true;
  if (p.get("marks") === "0") v.marks = false;
  if (p.get("flags") === "1") v.flags = true;
  v.cand = p.get("cand");
  const sel = p.get("sel")?.split(",").map(Number);
  if (sel?.length === 2 && sel.every(Number.isFinite) && Math.abs(sel[1]) <= 90)
    v.sel = { ra: ((sel[0] % 360) + 360) % 360, dec: sel[1] };
  const det = num(p.get("det"));
  if (det && det >= 1 && det <= 6) v.det = det;
  const dets = digits(p.get("dets"))?.filter((d) => d >= 1 && d <= 6);
  if (dets?.length) v.dets = dets;
  const hs = digits(p.get("hide"));
  if (hs) v.hiddenSeasons = hs;
  if (p.get("flagged") === "1") v.hideFlagged = false;
  if (p.get("inv") === "1") v.invert = true;
  if (p.get("fp") === "1") v.fp = true;
  const r = { ...v.render };
  const stretch = p.get("stretch");
  if (stretch && ["linear", "asinh", "log", "sqrt"].includes(stretch))
    r.stretch = stretch as RenderOpts["stretch"];
  const scale = p.get("scale");
  if (scale && ["auto", "noise", "detector", "frame"].includes(scale))
    r.scale = scale as Scale;
  r.lo = num(p.get("lo")) ?? r.lo;
  r.hi = num(p.get("hi")) ?? r.hi;
  r.sigLo = num(p.get("slo")) ?? r.sigLo;
  r.sigHi = num(p.get("shi")) ?? r.sigHi;
  v.render = r;
  const section = p.get("section") as NotebookSection | null;
  if (section && NOTEBOOK_SECTIONS.includes(section)) v.section = section;
  else if (v.fp) v.section = "spectrum";
  return v;
}

export function viewToSearch(v: ViewState): string {
  if (!v.target) return "";
  const t = v.target;
  const d = defaultView(t);
  const p = new URLSearchParams({ ra: t.ra.toFixed(6), dec: t.dec.toFixed(6) });
  if (t.label && !/^[\d\s.:+\-hdms]+$/i.test(t.label)) p.set("name", t.label);
  if (t.pm) {
    p.set("pmra", t.pm.pmra.toFixed(1));
    p.set("pmdec", t.pm.pmdec.toFixed(1));
    p.set("pmepoch", String(t.pm.epochMjd));
  }
  if (v.fov !== d.fov) p.set("fov", String(v.fov));
  if (v.mode !== d.mode) p.set("mode", v.mode);
  if (v.det !== d.det) p.set("det", String(v.det));
  if (v.bin !== d.bin) p.set("bin", String(v.bin));
  if (v.pair) p.set("pair", v.pair.join("-"));
  if (!v.diffCombined) p.set("dbin", "1");
  if (v.zmax !== d.zmax) p.set("zmax", String(v.zmax));
  if (v.dfloor !== d.dfloor) p.set("dfloor", String(v.dfloor));
  if (v.dgamma !== d.dgamma) p.set("dgamma", String(v.dgamma));
  if (v.diffLocal) p.set("dlocal", "1");
  if (v.gaia) p.set("gaia", "1");
  if (v.wise) p.set("wise", "1");
  if (!v.marks) p.set("marks", "0");
  if (v.flags) p.set("flags", "1");
  if (v.cand) p.set("cand", v.cand);
  if (v.sel) p.set("sel", `${v.sel.ra.toFixed(5)},${v.sel.dec.toFixed(5)}`);
  if (v.dets.length !== 6) p.set("dets", [...v.dets].sort().join(""));
  if (v.hiddenSeasons.length)
    p.set("hide", [...v.hiddenSeasons].sort().join(""));
  if (!v.hideFlagged) p.set("flagged", "1");
  if (v.invert) p.set("inv", "1");
  if (v.fp) p.set("fp", "1");
  if (v.section !== d.section || v.fp) p.set("section", v.section);
  const r = v.render;
  const dr = d.render;
  if (r.stretch !== dr.stretch) p.set("stretch", r.stretch);
  if (r.scale !== dr.scale) p.set("scale", r.scale);
  if (r.lo !== dr.lo) p.set("lo", String(r.lo));
  if (r.hi !== dr.hi) p.set("hi", String(r.hi));
  if (r.sigLo !== dr.sigLo) p.set("slo", String(r.sigLo));
  if (r.sigHi !== dr.sigHi) p.set("shi", String(r.sigHi));
  return `?${p.toString().replace(/%2C/gi, ",")}`;
}
