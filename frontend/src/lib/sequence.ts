// Builds the blink sequence for the current view, and the markers drawn on it.
import { hasAnalysis, type DiffMeta, type FieldResponse, type WaveBin } from './api'
import { diffItem, frameItem, rgbItem, stackItem, type BlinkItem } from './items'
import type { ViewState } from './viewState'

export interface Marker {
  x: number // grid pixel (0-based, +x west, +y north)
  y: number
  kind: 'mover' | 'variable' | 'appeared' | 'vanished' | 'uncertain' | 'fast'
  label?: string
  /** proper motion direction for movers (mas/yr, east/north) */
  pm?: [number, number]
  highlight?: boolean
}

export interface Sequence {
  items: BlinkItem[]
  /** stacks mode requested but the field is still downloading */
  stacksPending: boolean
  /** resolved selections (after clamping to what exists) */
  bins: WaveBin[]
  bin: WaveBin | null
  pairs: [number, number][]
  pair: [number, number] | null
}

const seasonLabeller = (field: FieldResponse) => (s: number) =>
  field.seasons.find((x) => x.season === s)?.start.slice(0, 7) ?? String(s)

/** Season pairs with a combined difference for a detector, deepest pair first. */
export function pairsFor(field: FieldResponse, det: number): [number, number][] {
  if (!hasAnalysis(field)) return []
  const depth = (s: number) =>
    field.stacks?.find((x) => x.detector === det && x.bin === null && x.visit == null && x.season === s)?.n_frames ?? 0
  return field.analysis.diffs
    .filter((d) => d.detector === det && d.bin === null)
    .map((d) => [d.season_a, d.season_b] as [number, number])
    .sort((p, q) => Math.min(depth(q[0]), depth(q[1])) - Math.min(depth(p[0]), depth(p[1])) || q[1] - q[0] - (p[1] - p[0]))
}

export function buildSequence(field: FieldResponse | null, view: ViewState): Sequence {
  const empty: Sequence = { items: [], stacksPending: false, bins: [], bin: null, pairs: [], pair: null }
  if (!field) return empty
  const label = seasonLabeller(field)
  const stacks = field.stacks ?? []
  const analysed = hasAnalysis(field)
  const bins = analysed ? field.analysis.bins.filter((b) => b.detector === view.det) : []
  const bin = bins.length ? bins[Math.min(view.bin, bins.length - 1)] : null
  const pairs = pairsFor(field, view.det)
  const pair =
    view.pair && pairs.some((p) => p[0] === view.pair![0] && p[1] === view.pair![1]) ? view.pair : (pairs[0] ?? null)
  const base = { ...empty, bins, bin, pairs, pair }
  const stackById = (id: string) => stacks.find((s) => s.id === id)

  if (view.mode === 'stacks' && stacks.length) {
    const items = stacks
      .filter((s) => s.detector === view.det && s.bin === null && s.visit == null && !view.hiddenSeasons.includes(s.season))
      .sort((a, b) => a.season - b.season)
      .map((s) => stackItem(s, label(s.season), view.render, view.flags))
    return { ...base, items }
  }
  if (view.mode === 'visits' && stacks.length) {
    const items = stacks
      .filter((s) => s.detector === view.det && !!s.visit && !view.hiddenSeasons.includes(s.season))
      .sort((a, b) => a.mjd - b.mjd)
      .map((s) => stackItem(s, label(s.season), view.render, view.flags))
    return { ...base, items }
  }
  if (view.mode === 'rgb' && stacks.length) {
    const seasons = [...new Set(stacks.filter((s) => s.bin === null && s.visit == null).map((s) => s.season))]
      .filter((s) => !view.hiddenSeasons.includes(s))
      .sort((a, b) => a - b)
    const items = seasons.map((n) =>
      rgbItem(field.key, n, label(n), stacks.filter((s) => s.season === n && s.bin === null && s.visit == null), view.render.sigHi),
    )
    return { ...base, items }
  }
  if (view.mode === 'bins' && bin) {
    const items = stacks
      .filter((s) => s.bin === bin.id && !view.hiddenSeasons.includes(s.season))
      .sort((a, b) => a.season - b.season)
      .map((s) => stackItem(s, label(s.season), view.render, view.flags))
    return { ...base, items }
  }
  if (view.mode === 'diff' && analysed && pair) {
    const [a, b] = pair
    let diff: DiffMeta | undefined
    let ids: [string, string]
    if (view.diffCombined || !bin) {
      diff = field.analysis.diffs.find((d) => d.id === `d${view.det}_s${a}-s${b}`)
      ids = [`s${a}_d${view.det}`, `s${b}_d${view.det}`]
    } else {
      diff = field.analysis.diffs.find((d) => d.id === `${bin.id}_s${a}-s${b}`)
      ids = [`s${a}_${bin.id}`, `s${b}_${bin.id}`]
    }
    const items: BlinkItem[] = []
    for (const id of ids) {
      const s = stackById(id)
      if (s) items.push(stackItem(s, label(s.season), view.render, view.flags))
    }
    if (diff) items.push(diffItem(diff, label, view.zmax, view.dfloor, view.dgamma, view.diffLocal))
    return { ...base, items }
  }
  if (view.mode !== 'frames' && !stacks.length) {
    // still downloading: show exposures as they arrive
    return { ...base, items: frameItems(field, view), stacksPending: true }
  }
  return { ...base, items: frameItems(field, view) }
}

function frameItems(field: FieldResponse, view: ViewState): BlinkItem[] {
  return field.frames
    .filter(
      (f) =>
        view.dets.includes(f.detector) &&
        !view.hiddenSeasons.includes(f.season) &&
        !(view.hideFlagged && f.target_quality === 'bad'),
    )
    .map((f) => frameItem(f, view.render, view.flags))
}

/** Markers to draw on a given image. */
export function markersFor(field: FieldResponse | null, view: ViewState, item: BlinkItem | undefined): Marker[] {
  if (!field || !item || !view.marks || !hasAnalysis(field)) return []
  const out: Marker[] = []
  if (item.kind === 'frame') {
    for (const fm of field.analysis.fast_movers) {
      const p = fm.points.find((q) => q.frame_id === item.id)
      if (p)
        out.push({
          x: p.x, y: p.y, kind: 'fast',
          label: fm.rate_arcsec_per_min ? `${fm.rate_arcsec_per_min.toFixed(1)}″/min` : fm.kind.replace('_', ' '),
        })
    }
    return out
  }
  for (const c of field.analysis.candidates) {
    // same rule as the detections list: weak-evidence candidates only when selected
    if (c.kind === 'static' || (c.evidence === 'weak' && c.id !== view.cand)) continue
    out.push({
      x: c.x, y: c.y, kind: c.kind,
      pm: c.pmra_masyr !== undefined && c.pmdec_masyr !== undefined ? [c.pmra_masyr, c.pmdec_masyr] : undefined,
      highlight: c.id === view.cand,
      label: c.id === view.cand ? c.kind : undefined,
    })
  }
  return out
}
