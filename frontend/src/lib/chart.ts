// Small helpers shared by the hand-written SVG charts (no charting dependency).

export type Scale = (v: number) => number

export const linear = (d0: number, d1: number, r0: number, r1: number): Scale => {
  const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0)
  return (v) => r0 + (v - d0) * k
}

export const logScale = (d0: number, d1: number, r0: number, r1: number): Scale => {
  const a = Math.log10(d0)
  const b = Math.log10(d1)
  return (v) => r0 + ((Math.log10(Math.max(v, d0 * 1e-6)) - a) / (b - a || 1)) * (r1 - r0)
}

export const isNum = (v: number | null | undefined): v is number => typeof v === 'number' && Number.isFinite(v)

export const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

export function median(xs: number[]): number {
  if (!xs.length) return NaN
  const s = [...xs].sort((a, b) => a - b)
  const m = s.length >> 1
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2
}

/** "Nice" tick positions covering [lo, hi]; the first/last tick are the padded domain ends. */
export function niceTicks(lo: number, hi: number, target = 5): number[] {
  if (!(hi > lo)) {
    const c = lo || 1
    lo = c - Math.abs(c) * 0.5
    hi = c + Math.abs(c) * 0.5
  }
  const raw = (hi - lo) / Math.max(1, target)
  const mag = 10 ** Math.floor(Math.log10(raw))
  const f = raw / mag
  const step = (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * mag
  const start = Math.floor(lo / step + 1e-9) * step
  const out: number[] = []
  for (let v = start; v <= hi + step * 0.999; v += step) {
    out.push(Math.abs(v) < step * 1e-9 ? 0 : Number(v.toPrecision(12)))
    if (out.length > 60) break
  }
  // Stop at the first tick at or above hi.
  const cut = out.findIndex((v) => v >= hi - step * 1e-9)
  return cut >= 0 ? out.slice(0, cut + 1) : out
}

/** Ticks for a log axis: decades, plus 2 and 5 when the span is short. */
export function logTicks(lo: number, hi: number): number[] {
  const out: number[] = []
  const e0 = Math.floor(Math.log10(lo))
  const e1 = Math.ceil(Math.log10(hi))
  const mult = e1 - e0 <= 2 ? [1, 2, 5] : [1]
  for (let e = e0; e <= e1; e++) {
    for (const m of mult) {
      const v = m * 10 ** e
      if (v >= lo * (1 - 1e-9) && v <= hi * (1 + 1e-9)) out.push(v)
    }
  }
  return out
}

/** Axis tick label: decimals follow the tick step, thousands compact to "k". */
export function fmtTick(v: number, step?: number): string {
  if (v === 0) return '0'
  const a = Math.abs(v)
  if (a >= 1e4) return `${Number((v / 1e3).toPrecision(3))}k`
  const dec = step && step > 0 ? Math.max(0, Math.min(4, -Math.floor(Math.log10(step) + 1e-9))) : a < 1 ? 2 : a < 10 ? 1 : 0
  return v.toFixed(dec)
}

/** Flux readout for tooltips: 3 significant figures at most, no scientific notation. */
export function fmtFlux(v: number | null | undefined): string {
  if (!isNum(v)) return '–'
  const a = Math.abs(v)
  return a >= 100 ? v.toFixed(0) : a >= 10 ? v.toFixed(1) : a >= 1 ? v.toFixed(2) : v.toFixed(3)
}

export const fmtFluxErr = (v: number | null | undefined, e: number | null | undefined) =>
  isNum(e) ? `${fmtFlux(v)} ± ${fmtFlux(e)}` : fmtFlux(v)

/** Inverse-variance weighted mean; null when there is nothing with a usable error. */
export function weightedMean(vals: { v: number; e: number }[]): number | null {
  let sw = 0
  let swv = 0
  for (const { v, e } of vals) {
    if (!(e > 0)) continue
    const w = 1 / (e * e)
    sw += w
    swv += w * v
  }
  return sw > 0 ? swv / sw : null
}
