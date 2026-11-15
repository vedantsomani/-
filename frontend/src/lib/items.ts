// A BlinkItem is anything the player can show: a single exposure or a co-add.
import type { DiffMeta, FrameMeta, RenderOpts, StackMeta } from './api'
import { apiUrl, renderQuery } from './api'

export interface BlinkItem {
  kind: 'frame' | 'stack' | 'diff' | 'rgb'
  id: string
  url: string // fully-qualified PNG url incl. render params
  /** Transparent flag / coverage overlay drawn on top; only set while "Show flagged pixels" is on. */
  overlayUrl?: string
  mjd: number
  mjdMin?: number
  mjdMax?: number
  season: number
  detector: number
  wavelengthUm: number | null
  title: string // first overlay line (date)
  subtitle: string // detector · wavelength
  note: string // obs id / n exposures
  warning?: { level: 'bad' | 'suspect'; text: string }
}

const INFORMATIONAL = new Set(['SOURCE', 'FULLSAMPLE', 'DICHROIC'])

export function frameItem(f: FrameMeta, render: RenderOpts, overlay = false): BlinkItem {
  const flags = f.target_flags.filter((x) => !INFORMATIONAL.has(x))
  return {
    kind: 'frame',
    id: f.id,
    url: apiUrl(`${f.png}?${renderQuery(render, 'frame')}`),
    overlayUrl: overlay ? apiUrl(f.png.replace(/\.png$/, '/flags.png')) : undefined,
    mjd: f.mjd,
    season: f.season,
    detector: f.detector,
    wavelengthUm: f.wavelength_um,
    title: `${f.date?.replace('T', ' ')} UTC`,
    subtitle: `D${f.detector} · ${f.wavelength_um?.toFixed(3)} µm${f.bandwidth_um ? ` (Δλ ${f.bandwidth_um.toFixed(3)})` : ''}`,
    note: f.obs_id,
    warning: f.target_quality !== 'good' && flags.length ? { level: f.target_quality as 'bad' | 'suspect', text: flags.join(' ') } : undefined,
  }
}

export function stackItem(s: StackMeta, seasonLabel: string, render: RenderOpts, overlay = false): BlinkItem {
  const lam =
    s.wavelength_min_um && s.wavelength_max_um
      ? `${s.wavelength_min_um.toFixed(2)}–${s.wavelength_max_um.toFixed(2)} µm`
      : ''
  return {
    kind: 'stack',
    id: s.id,
    url: apiUrl(`${s.png}?${renderQuery(render, 'stack')}`),
    overlayUrl: overlay ? apiUrl(s.png.replace(/\.png$/, '/coverage.png')) : undefined,
    mjd: s.mjd,
    mjdMin: s.mjd_min,
    mjdMax: s.mjd_max,
    season: s.season,
    detector: s.detector,
    wavelengthUm: s.wavelength_um,
    // per-visit stacks are labelled by their own date+time; season stacks by the season
    title: s.visit
      ? `${s.date.replace('T', ' ').slice(0, 16)} UTC`
      : `Season ${seasonLabel} · mean ${s.date.slice(0, 10)}`,
    subtitle: s.visit ? `D${s.detector} visit · ${lam}` : `D${s.detector} stack · ${lam}`,
    note: `${s.n_frames} exposure${s.n_frames === 1 ? '' : 's'}${s.depth_gain ? ` · ${s.depth_gain}× deeper` : ''}`,
    warning: s.n_at_target === 0 ? { level: 'bad', text: 'no usable pixels at the target' } : undefined,
  }
}

export function diffItem(d: DiffMeta, label: (s: number) => string, zmax: number, dfloor: number, dgamma: number, local: boolean): BlinkItem {
  const lam =
    d.wavelength_a_um && d.wavelength_b_um
      ? ` · λ̄ ${d.wavelength_a_um.toFixed(2)} → ${d.wavelength_b_um.toFixed(2)} µm`
      : ''
  return {
    kind: 'diff',
    id: d.id,
    url: apiUrl(`${d.png}?zmax=${zmax}&floor=${dfloor}&gamma=${dgamma}&norm=${local ? 'local' : 'global'}`),
    mjd: d.mjd_b,
    season: d.season_b,
    detector: d.detector,
    wavelengthUm: d.wavelength_b_um,
    title: `Difference ${label(d.season_b)} − ${label(d.season_a)} (Δt ${Math.round(d.dt_days)} d)`,
    subtitle: `D${d.detector} ${d.bin ? `bin ${d.bin.split('b')[1]}` : `all ${d.n_bins} λ-bins`}${lam}`,
    note: `orange = brighter later · blue = fainter later · from ${dfloor}σ · ±${zmax}σ`,
  }
}

/** False-colour image of one season: blue D1+D2, green D3+D4, red D5+D6 (stacks of that season). */
export const RGB_LEGEND = 'blue 0.75–1.6, green 1.6–3.8, red 3.8–5.0 µm'

export function rgbItem(fieldKey: string, season: number, seasonLabel: string, stacks: StackMeta[], sigHi: number): BlinkItem {
  const mjds = stacks.map((s) => s.mjd)
  const mean = mjds.reduce((a, b) => a + b, 0) / Math.max(mjds.length, 1)
  const have = new Set(stacks.map((s) => s.detector))
  const missing = [1, 2, 3, 4, 5, 6].filter((d) => !have.has(d))
  const nExp = stacks.reduce((a, s) => a + s.n_frames, 0)
  return {
    kind: 'rgb',
    id: `rgb_s${season}`,
    url: apiUrl(`/api/fields/${fieldKey}/rgb/s${season}.png?sig_hi=${sigHi}`),
    mjd: mean,
    mjdMin: Math.min(...mjds),
    mjdMax: Math.max(...mjds),
    season,
    detector: 0,
    wavelengthUm: null,
    title: `Season ${seasonLabel} · mean ${stacks[0]?.date.slice(0, 10) ?? ''}`,
    subtitle: `Colour · ${have.size === 6 ? 'D1–D6' : [...have].sort().map((d) => `D${d}`).join(' ')} stacks`,
    note: `${nExp} exposures in 3 colour channels`,
    warning: missing.length ? { level: 'suspect', text: `no ${missing.map((d) => `D${d}`).join(' ')} this season` } : undefined,
  }
}
