import { useEffect, useState } from 'react'
import { formatDec, formatRa } from '../lib/coords'
import type { Snap } from '../lib/wcs'

interface Props {
  snap: Snap
  /** state of the Gaia lookup, which can still change what the pick snaps to */
  gaiaStatus: 'ok' | 'loading' | 'failed'
  onInvestigate: () => void
  onClear: () => void
}

/** What the pick snapped to, in one line. */
function Description({ snap, gaiaStatus }: Pick<Props, 'snap' | 'gaiaStatus'>) {
  if (snap.kind === 'candidate') {
    const c = snap.cand
    return (
      <p>
        <span className="font-semibold text-white">{c.is_target ? 'The target' : `${c.kind.replace('_', ' ')} candidate`}</span>
        <span className="text-ink-300"> · {c.evidence} evidence · peak {c.max_z.toFixed(1)}σ</span>
      </p>
    )
  }
  if (snap.kind === 'gaia') {
    const { g_mag: g, pm_total_masyr: mu, source_id: id } = snap.src
    return (
      <p className="break-words">
        <span className="font-semibold text-white">Gaia {String(id)}</span>
        <span className="text-ink-300">
          {' '}· G {g === null ? '?' : g.toFixed(2)} · {mu === null ? 'motion unknown' : `μ ${mu.toFixed(mu < 10 ? 1 : 0)} mas/yr`}
        </span>
      </p>
    )
  }
  if (snap.kind === 'catwise') {
    const { w1, w2, w1_w2: c, pm_total_masyr: mu, pm_snr: snr, source_name: id } = snap.src
    const mag = (v: number | null) => (v === null ? '?' : v.toFixed(2))
    return (
      <p className="break-words">
        <span className="font-semibold text-white">CatWISE {id}</span>
        <span className="text-ink-300">
          {' '}· W1 {mag(w1)} · W2 {mag(w2)}{c !== null && ` · W1−W2 ${c.toFixed(2)}`} · μ {mu.toFixed(mu < 10 ? 1 : 0)} mas/yr
          {snr !== null && ` (S/N ${snr.toFixed(0)})`}
        </span>
      </p>
    )
  }
  return (
    <p className="text-ink-300">
      {gaiaStatus === 'loading' ? 'Checking Gaia…' : gaiaStatus === 'failed' ? 'No source found (Gaia could not be reached).' : 'No catalogued source here.'}
    </p>
  )
}

/** Popover for the picked position: what is there, and what to do with it. */
export function SelectionCard({ snap, gaiaStatus, onInvestigate, onClear }: Props) {
  const [copied, setCopied] = useState<'idle' | 'ok' | 'fail'>('idle')
  useEffect(() => {
    if (copied === 'idle') return
    const t = window.setTimeout(() => setCopied('idle'), 2000)
    return () => window.clearTimeout(t)
  }, [copied])

  const text = `${snap.ra.toFixed(5)} ${snap.dec.toFixed(5)}`
  async function copy() {
    try {
      await navigator.clipboard.writeText(text)
      setCopied('ok')
    } catch {
      setCopied('fail')
    }
  }

  return (
    <div
      role="group"
      aria-label="Selected source"
      className="flex flex-col gap-2 rounded-md border border-line bg-ink-900 p-3 text-xs shadow-[0_8px_24px_rgb(0_0_0/0.5)]"
      onKeyDown={(e) => { if (e.key === 'Escape') onClear() }}
    >
      <div className="flex flex-col gap-0.5">
        <p className="font-mono text-slate-100" title={`${formatRa(snap.ra)} ${formatDec(snap.dec)}`}>
          {snap.ra.toFixed(5)}° {snap.dec >= 0 ? '+' : '−'}{Math.abs(snap.dec).toFixed(5)}°
        </p>
        <Description snap={snap} gaiaStatus={gaiaStatus} />
      </div>
      <div className="flex flex-wrap gap-2">
        <button type="button" className="btn btn-primary btn-sm" onClick={onInvestigate}>
          Investigate this source
        </button>
        <button type="button" className="btn btn-sm" onClick={copy}>
          {copied === 'ok' ? 'Copied' : copied === 'fail' ? 'Copy failed' : 'Copy coordinates'}
        </button>
        <button type="button" className="btn btn-sm" onClick={onClear}>Clear</button>
      </div>
      <p className="text-[11px] leading-snug text-ink-500">
        Investigating re-centres the field on this source; cached exposures are reused when the new centre is close,
        otherwise a new download starts.
      </p>
      <span className="sr-only" role="status">{copied === 'ok' ? 'Coordinates copied.' : ''}</span>
    </div>
  )
}
