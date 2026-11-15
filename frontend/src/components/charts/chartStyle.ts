// Colours for the fingerprint charts: the validated dark-surface categorical slots (blue, orange,
// aqua, violet, yellow; checked with the dataviz validator against the app's ink-900 surface).
// Every series is also told apart by marker SHAPE, so identity never depends on hue alone.

export type MarkerShape = 'circle' | 'square' | 'diamond' | 'triangle' | 'triangleDown' | 'cross'

export const SERIES_COLOR = {
  deblended: '#3987e5',
  naive: '#d95926',
  /** Naive aperture while deblended points are coloured by season (orange would collide with season colours). */
  naiveNeutral: '#c3cbe0',
  neighbour: '#8892ad',
} as const

/** Season identity: colour + shape, assigned by season order and never cycled. */
export const SEASON_STYLES: { color: string; shape: MarkerShape }[] = [
  { color: '#199e70', shape: 'circle' },
  { color: '#9085e9', shape: 'triangle' },
  { color: '#c98500', shape: 'triangleDown' },
  { color: '#d55181', shape: 'cross' },
]
export const SEASON_OVERFLOW = { color: '#8892ad', shape: 'circle' as MarkerShape }
