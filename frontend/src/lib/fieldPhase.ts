// What the data pipeline is doing, for the status line, and relative times for the provenance line.
import type { FieldResponse } from './api'

export function relativeTime(iso: string, now: number): string {
  const s = Math.max(0, (now - new Date(iso).getTime()) / 1000)
  if (Number.isNaN(s)) return 'a while ago'
  if (s < 45) return 'just now'
  const unit = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'} ago`
  if (s < 3600) return unit(Math.max(1, Math.round(s / 60)), 'minute')
  if (s < 86400) return unit(Math.round(s / 3600), 'hour')
  return unit(Math.round(s / 86400), 'day')
}

export interface Phase {
  busy: boolean
  /** Short inline label for the status line. */
  label: string
  /** Fraction done (0-100) or null when indeterminate. */
  pct: number | null
  /** Sentence for screen readers (phase changes only). */
  spoken: string
}

/** What the data pipeline is doing, as one short line plus a progress fraction. */
export function fieldPhase(field: FieldResponse | null, error: string | null, reconnecting: boolean): Phase {
  const nFailed = field ? Object.keys(field.failed).length : 0
  if (error) return { busy: false, label: 'Loading failed', pct: null, spoken: 'Loading failed.' }
  if (!field)
    return reconnecting
      ? { busy: true, label: 'Reconnecting…', pct: null, spoken: 'Connection interrupted, retrying.' }
      : { busy: true, label: 'Contacting server…', pct: null, spoken: 'Contacting the server.' }
  if (field.status === 'pending' || field.status === 'searching')
    return { busy: true, label: 'Searching IRSA…', pct: null, spoken: 'Searching the IRSA archive for SPHEREx images.' }
  if (field.status === 'processing') {
    const done = field.done + nFailed
    return {
      busy: true,
      label: `Downloading ${done} of ${field.total}`,
      pct: field.total ? (done / field.total) * 100 : null,
      spoken: 'Downloading and aligning exposures.',
    }
  }
  if (field.status === 'stacking') return { busy: true, label: 'Stacking and analysing…', pct: null, spoken: 'Combining exposures and checking for changes.' }
  if (field.status === 'complete') {
    return field.total === 0
      ? { busy: false, label: 'No coverage', pct: null, spoken: 'No SPHEREx images cover this position.' }
      : { busy: false, label: `${field.frames.length} exposures · ${field.seasons.length} seasons`, pct: null, spoken: `${field.frames.length} images are ready.` }
  }
  return { busy: false, label: '', pct: null, spoken: '' }
}
