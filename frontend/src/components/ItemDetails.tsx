import type { FieldResponse } from '../lib/api'
import { detectorColor } from '../lib/constants'
import type { BlinkItem } from '../lib/items'

const Row = ({ k, children }: { k: string; children: React.ReactNode }) => (
  <>
    <dt className="text-ink-500">{k}</dt>
    <dd className="min-w-0 truncate">{children}</dd>
  </>
)

export function ItemDetails({ item, field }: { item: BlinkItem; field: FieldResponse }) {
  if (item.kind === 'diff') {
    const d = field.analysis && 'diffs' in field.analysis ? field.analysis.diffs.find((x) => x.id === item.id) : undefined
    if (!d) return null
    return (
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 font-mono text-xs">
        <Row k="Difference">D{d.detector} · season {d.season_b} − {d.season_a}</Row>
        <Row k="Baseline">{d.dt_days.toFixed(0)} days</Row>
        <Row k="λ̄ (A → B)">{d.wavelength_a_um?.toFixed(3)} → {d.wavelength_b_um?.toFixed(3)} µm</Row>
        <Row k="Built from">{d.bin ? `bin ${d.bin}` : `${d.n_bins} wavelength-matched bins`}</Row>
        <Row k="Noise rescale">×{d.noise_scale} (empirical / VARIANCE)</Row>
      </dl>
    )
  }
  if (item.kind === 'stack') {
    const s = field.stacks?.find((x) => x.id === item.id)
    if (!s) return null
    return (
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 font-mono text-xs">
        <Row k="Stack">
          <span style={{ color: detectorColor(s.detector) }}>D{s.detector}</span> · season {s.season}
        </Row>
        <Row k="Exposures">{s.n_frames} ({s.n_at_target} usable at target)</Row>
        <Row k="Dates">{s.mjd_min.toFixed(2)} – {s.mjd_max.toFixed(2)} MJD</Row>
        <Row k="Mean epoch">{s.date.replace('T', ' ')}</Row>
        {s.bin && <Row k="λ bin">{s.bin}</Row>}
        <Row k="λ range">{s.wavelength_min_um?.toFixed(3)} – {s.wavelength_max_um?.toFixed(3)} µm</Row>
        <Row k="λ at target">{s.wavelength_um?.toFixed(3)} µm (weighted)</Row>
        <Row k="Noise">{s.noise_mjysr?.toExponential(2)} MJy/sr</Row>
        <Row k="Depth gain">{s.depth_gain ? `${s.depth_gain}× vs one exposure (√N = ${Math.sqrt(s.n_frames).toFixed(1)})` : '—'}</Row>
      </dl>
    )
  }
  const f = field.frames.find((x) => x.id === item.id)
  if (!f) return null
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 font-mono text-xs">
      <Row k="Detector"><span style={{ color: detectorColor(f.detector) }}>D{f.detector}</span></Row>
      <Row k="λ / Δλ">{f.wavelength_um?.toFixed(4)} / {f.bandwidth_um?.toFixed(4)} µm</Row>
      <Row k="MJD">{f.mjd.toFixed(5)}</Row>
      <Row k="Exposure">{f.exptime_s?.toFixed(1)} s</Row>
      <Row k="Visit">{f.visit}</Row>
      <Row k="Zodi">{f.zodi_mjysr?.toFixed(3)} MJy/sr (subtracted)</Row>
      <Row k="Noise">{f.noise_mjysr?.toFixed(4)} MJy/sr</Row>
      <Row k="Coverage">{(f.coverage * 100).toFixed(0)}%</Row>
      <Row k="Target flags">{f.target_flags.join(', ') || '—'}</Row>
      <Row k="Artifacts">
        {Object.entries(f.artifact_frac).map(([k, v]) => `${k} ${(v * 100).toFixed(1)}%`).join(', ') || '—'}
      </Row>
      <Row k="Source">
        <a href={f.source_url} className="underline decoration-ink-700 hover:text-accent" target="_blank" rel="noreferrer">
          {f.collection} L2 image
        </a>
      </Row>
    </dl>
  )
}
