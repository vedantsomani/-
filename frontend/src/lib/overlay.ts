// Gaia catalogue overlay: response types (GET /api/overlay) and what to draw on one image.
import type { BlinkItem } from './items'

export interface GaiaSource {
  source_id: string | number
  g_mag: number | null
  variable: boolean
  /** proper motion, mas/yr (east, north); null for two-parameter solutions */
  pmra_masyr: number | null
  pmdec_masyr: number | null
  pm_total_masyr: number | null
  /** grid pixel at MJD 61000 (0-based, +x west, +y north) */
  x: number
  y: number
  /** grid pixel at each season's mean epoch, keyed by season number */
  seasons: Record<string, [number, number]>
}

/** A CatWISE2020 source (red or moving, or matched to a candidate); motions are mas/yr, mags are Vega. */
export interface CatwiseSource {
  source_name: string
  ra: number
  dec: number
  w1: number | null
  w2: number | null
  w1_w2: number | null
  pmra_masyr: number
  pmdec_masyr: number
  pm_total_masyr: number
  /** S/N of the total motion; the motion is only trusted above CATWISE_PM_SNR */
  pm_snr: number | null
  /** grid pixel at MJD 61000 and at each season's mean epoch (motion applied only when significant) */
  x: number
  y: number
  seasons: Record<string, [number, number]>
}

export interface CatwiseMatch {
  source_name: string
  separation_arcsec: number
  w1: number | null
  w2: number | null
  w1_w2: number | null
  pm_total_masyr: number
  pm_snr: number | null
  pm_agrees: boolean | null
}

export interface OverlayCandidate {
  id: string
  label: string
  label_note?: string
  candidate_new_object: boolean
  nearest_gaia_arcsec: number | null
  catwise_match?: CatwiseMatch | null
  /** false when the CatWISE query failed, so "no match" says nothing */
  catwise_checked?: boolean
}

export interface OverlayData {
  wise?: WiseOverlay | null
  status: 'complete'
  catalog: { name: string; ok: boolean; note?: string; n_in_field?: number; n_returned?: number }
  sources: GaiaSource[]
  /** CatWISE2020 query status and sources; absent on older servers */
  catwise?: { name: string; ok: boolean; note?: string | null; n_in_field?: number; n_returned?: number }
  catwise_sources?: CatwiseSource[]
  candidates: Record<string, OverlayCandidate>
  notes: string[]
}

export type OverlayResponse = OverlayData | { status: 'processing' } | { status: 'error'; error: string }

/** One Gaia source ready to draw: grid position plus proper motion. */
export interface GaiaMark {
  x: number
  y: number
  /** mas/yr east, north; only set for sources faster than FAST_MASYR */
  pm?: [number, number]
  /** total proper motion, arcsec/yr */
  speed?: number
}

export const FAST_MASYR = 100
export const MAX_GAIA_MARKS = 40
export const CATWISE_PM_SNR = 5
export const CATWISE_RED_W1W2 = 1.5
export const MAX_CATWISE_MARKS = 12

export const catwiseMoving = (s: CatwiseSource) =>
  s.pm_snr !== null && s.pm_snr > CATWISE_PM_SNR && s.pm_total_masyr > FAST_MASYR
export const catwiseRed = (s: CatwiseSource) => s.w1_w2 !== null && s.w1_w2 > CATWISE_RED_W1W2
/** The CatWISE sources that are drawn (and can be picked): red or moving. */
export const catwiseShown = (s: CatwiseSource) => catwiseMoving(s) || catwiseRed(s)

/** Gaia sources for the image on screen, at that image's season (MJD 61000 position when the season is unknown).
 *  Fast movers come first, then the brightest sources; at most MAX_GAIA_MARKS, all inside the grid. */
export function gaiaMarksFor(data: OverlayData | null | undefined, item: BlinkItem | undefined, gridN: number): GaiaMark[] {
  if (!data?.catalog.ok || !item) return []
  const fast: GaiaMark[] = []
  const rest: GaiaMark[] = []
  for (const s of data.sources) {
    const [x, y] = s.seasons[String(item.season)] ?? [s.x, s.y]
    if (x < -0.5 || y < -0.5 || x > gridN - 0.5 || y > gridN - 0.5) continue
    const isFast = s.pm_total_masyr !== null && s.pm_total_masyr > FAST_MASYR && s.pmra_masyr !== null && s.pmdec_masyr !== null
    if (isFast) fast.push({ x, y, pm: [s.pmra_masyr as number, s.pmdec_masyr as number], speed: (s.pm_total_masyr as number) / 1000 })
    else rest.push({ x, y }) // the response lists sources brightest first
  }
  return [...fast, ...rest].slice(0, MAX_GAIA_MARKS)
}

/** Red or moving CatWISE sources for the image on screen (orange diamonds), movers first, inside the grid. */
export function catwiseMarksFor(data: OverlayData | null | undefined, item: BlinkItem | undefined, gridN: number): GaiaMark[] {
  if (!data?.catwise?.ok || !item) return []
  const fast: GaiaMark[] = []
  const rest: GaiaMark[] = []
  for (const s of data.catwise_sources ?? []) {
    if (!catwiseShown(s)) continue
    const [x, y] = s.seasons[String(item.season)] ?? [s.x, s.y]
    if (x < -0.5 || y < -0.5 || x > gridN - 0.5 || y > gridN - 0.5) continue
    if (catwiseMoving(s)) fast.push({ x, y, pm: [s.pmra_masyr, s.pmdec_masyr], speed: s.pm_total_masyr / 1000 })
    else rest.push({ x, y })
  }
  return [...fast, ...rest].slice(0, MAX_CATWISE_MARKS)
}

export interface WiseCatalogInfo {
  name: string
  ok: boolean
  note?: string
  ref_epoch_mjd: number
  n_in_field?: number
  n_returned?: number
}

export interface WiseSource {
  source_id: string
  designation: string
  ra: number
  dec: number
  pmra_masyr: number | null
  pmdec_masyr: number | null
  pm_total_masyr: number | null
  w1: number | null
  w2: number | null
  w3: number | null
  w4: number | null
  variable: boolean
  x: number
  y: number
  seasons: Record<string, [number, number]>
}

export interface WiseCandidate {
  id: string
  designation: string | null
  separation_arcsec: number | null
  w1: number | null
  w2: number | null
  pm_source: 'self' | 'candidate' | null
  pm_agrees: boolean | null
  variable: boolean
  within_novelty_radius: boolean
}

export interface WiseOverlay {
  status: 'complete'
  catalog: WiseCatalogInfo
  sources: WiseSource[]
  candidates: Record<string, WiseCandidate>
}


export const MAX_WISE_MARKS = 40
interface HasPosition {
  x: number
  y: number
  /** grid pixel at each season's mean epoch, keyed by season number */
  seasons?: Record<string, [number, number]>
  pmra_masyr?: number | null
  pmdec_masyr?: number | null
  pm_total_masyr?: number | null
}

function marksFor(sources: Array<HasPosition>, item: BlinkItem | undefined, gridN: number, maxMarks: number): GaiaMark[] {
  if (!item) return []
  const fast: GaiaMark[] = []
  const rest: GaiaMark[] = []
  for (const s of sources) {
    const seasons = s.seasons ? s.seasons[String(item.season)] : undefined
    const [x, y] = seasons ?? [s.x, s.y]
    if (x < -0.5 || y < -0.5 || x > gridN - 0.5 || y > gridN - 0.5) continue
    const isFast = s.pm_total_masyr !== null && s.pm_total_masyr !== undefined && s.pm_total_masyr > FAST_MASYR && s.pmra_masyr !== null && s.pmdec_masyr !== null
    if (isFast) fast.push({ x, y, pm: [s.pmra_masyr as number, s.pmdec_masyr as number], speed: (s.pm_total_masyr as number) / 1000 })
    else rest.push({ x, y })
  }
  return [...fast, ...rest].slice(0, maxMarks)
}


/** AllWISE sources for the image on screen (same drawing convention; at most MAX_WISE_MARKS). */
export function wiseMarksFor(data: OverlayData | null | undefined, item: BlinkItem | undefined, gridN: number): GaiaMark[] {
  if (!data?.wise?.catalog.ok || !item) return []
  return marksFor(data.wise.sources, item, gridN, MAX_WISE_MARKS)
}
