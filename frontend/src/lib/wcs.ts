// Grid pixel <-> sky for the field's TAN grid (FieldResponse.grid.wcs), plus "what is under this click".
// Grid pixels are 0-based, +x west, +y north (FITS pixel = grid pixel + 1).
import type { Candidate } from './api'
import { SPHEREX_REF_MJD } from './coords'
import { catwiseMoving, catwiseShown, FAST_MASYR, type CatwiseSource, type GaiaSource } from './overlay'
import type { Target } from './viewState'

export interface TanWcs {
  ra0: number // deg
  dec0: number
  crpix: [number, number] // FITS (1-based)
  /** linear map from pixel offset to tangent-plane degrees: [[a, b], [c, d]] */
  m: [[number, number], [number, number]]
}

const D2R = Math.PI / 180

/** Builds the projection from the header, or null when it is not a plain RA/Dec TAN header. */
export function tanFromHeader(h: Record<string, unknown> | undefined): TanWcs | null {
  if (!h) return null
  const get = (k: string, d?: number) => (typeof h[k] === 'number' && Number.isFinite(h[k]) ? (h[k] as number) : d)
  const ra0 = get('CRVAL1')
  const dec0 = get('CRVAL2')
  const p1 = get('CRPIX1')
  const p2 = get('CRPIX2')
  if (ra0 === undefined || dec0 === undefined || p1 === undefined || p2 === undefined) return null
  if (!String(h.CTYPE1 ?? 'RA---TAN').endsWith('TAN')) return null
  let m: TanWcs['m']
  if (get('CD1_1') !== undefined) {
    m = [[get('CD1_1', 0)!, get('CD1_2', 0)!], [get('CD2_1', 0)!, get('CD2_2', 0)!]]
  } else {
    const [d1, d2] = [get('CDELT1', 1)!, get('CDELT2', 1)!]
    m = [
      [d1 * get('PC1_1', 1)!, d1 * get('PC1_2', 0)!],
      [d2 * get('PC2_1', 0)!, d2 * get('PC2_2', 1)!],
    ]
  }
  return { ra0, dec0, crpix: [p1, p2], m }
}

/** Grid pixel -> sky (deg): exact inverse gnomonic projection. */
export function pixToSky(w: TanWcs, x: number, y: number): { ra: number; dec: number } {
  const u = x + 1 - w.crpix[0]
  const v = y + 1 - w.crpix[1]
  const xi = (w.m[0][0] * u + w.m[0][1] * v) * D2R
  const eta = (w.m[1][0] * u + w.m[1][1] * v) * D2R
  const d0 = w.dec0 * D2R
  const den = Math.cos(d0) - eta * Math.sin(d0)
  const ra = w.ra0 + Math.atan2(xi, den) / D2R
  const dec = Math.atan2(Math.sin(d0) + eta * Math.cos(d0), Math.hypot(xi, den)) / D2R
  return { ra: ((ra % 360) + 360) % 360, dec }
}

/** Sky (deg) -> grid pixel. Null for points on the far side of the sky. */
export function skyToPix(w: TanWcs, ra: number, dec: number): { x: number; y: number } | null {
  const d0 = w.dec0 * D2R
  const d = dec * D2R
  const dra = (ra - w.ra0) * D2R
  const cosc = Math.sin(d0) * Math.sin(d) + Math.cos(d0) * Math.cos(d) * Math.cos(dra)
  if (cosc <= 1e-9) return null
  const xi = (Math.cos(d) * Math.sin(dra)) / cosc / D2R // deg
  const eta = (Math.cos(d0) * Math.sin(d) - Math.sin(d0) * Math.cos(d) * Math.cos(dra)) / cosc / D2R
  const [[a, b], [c, e]] = w.m
  const det = a * e - b * c
  const u = (e * xi - b * eta) / det
  const v = (-c * xi + a * eta) / det
  return { x: u + w.crpix[0] - 1, y: v + w.crpix[1] - 1 }
}

export type Snap =
  | { kind: 'candidate'; cand: Candidate; ra: number; dec: number; x: number; y: number }
  | { kind: 'gaia'; src: GaiaSource; ra: number; dec: number; x: number; y: number }
  | { kind: 'catwise'; src: CatwiseSource; ra: number; dec: number; x: number; y: number }
  | { kind: 'none'; ra: number; dec: number; x: number; y: number }

/** Candidates the user can see on the image (same rule as the markers). */
export const visibleCandidates = (cands: Candidate[] | undefined, selectedId: string | null) =>
  (cands ?? []).filter((c) => c.kind !== 'static' && (c.evidence !== 'weak' || c.id === selectedId))

/**
 * What lies within `tolPx` of grid pixel (x, y): the nearest candidate, else the nearest Gaia or (red / moving) CatWISE source, else the raw point.
 * With `season`, Gaia sources are compared at that season's position (what is drawn); the snapped point is always the
 * MJD 61000 position, which is where a proper-motion target is centred.
 */
export function snapNear(
  w: TanWcs, x: number, y: number, tolPx: number, cands: Candidate[], gaia: GaiaSource[] | null, season?: number,
  catwise: CatwiseSource[] | null = null,
): Snap {
  let best: Snap | null = null
  let bd = tolPx
  for (const c of cands) {
    const d = Math.hypot(c.x - x, c.y - y)
    if (d <= bd) { bd = d; best = { kind: 'candidate', cand: c, ra: c.ra, dec: c.dec, x: c.x, y: c.y } }
  }
  if (best) return best
  bd = tolPx
  for (const s of gaia ?? []) {
    const [sx, sy] = (season !== undefined && s.seasons[String(season)]) || [s.x, s.y]
    const d = Math.hypot(sx - x, sy - y)
    if (d <= bd) { bd = d; best = { kind: 'gaia', src: s, ...pixToSky(w, s.x, s.y), x: s.x, y: s.y } }
  }
  for (const s of catwise ?? []) { // only the red / moving ones that are drawn
    if (!catwiseShown(s)) continue
    const [sx, sy] = (season !== undefined && s.seasons[String(season)]) || [s.x, s.y]
    const d = Math.hypot(sx - x, sy - y)
    if (d <= bd) { bd = d; best = { kind: 'catwise', src: s, ...pixToSky(w, s.x, s.y), x: s.x, y: s.y } }
  }
  return best ?? { kind: 'none', ...pixToSky(w, x, y), x, y }
}

/** The target to load when the user chooses "Investigate this source" for a snapped pick. */
export function targetFromSnap(s: Snap): Target {
  if (s.kind === 'gaia') {
    // Slower stars move < 0.1 arcsec over the survey, so a motion marker would only add noise ("Moves 0.0 a year").
    const { pmra_masyr: pmra, pmdec_masyr: pmdec, pm_total_masyr: mu } = s.src
    return {
      ra: s.ra, dec: s.dec, label: `Gaia ${s.src.source_id}`,
      pm: pmra !== null && pmdec !== null && mu !== null && mu >= FAST_MASYR ? { pmra, pmdec, epochMjd: SPHEREX_REF_MJD } : undefined,
    }
  }
  if (s.kind === 'catwise') {
    const { pmra_masyr: pmra, pmdec_masyr: pmdec } = s.src
    return {
      ra: s.ra, dec: s.dec, label: `CatWISE ${s.src.source_name}`,
      pm: catwiseMoving(s.src) ? { pmra, pmdec, epochMjd: SPHEREX_REF_MJD } : undefined,
    }
  }
  const label = s.kind === 'candidate' ? `Candidate ${s.cand.kind}` : `${s.ra.toFixed(5)} ${s.dec.toFixed(5)}`
  return { ra: s.ra, dec: s.dec, label }
}
