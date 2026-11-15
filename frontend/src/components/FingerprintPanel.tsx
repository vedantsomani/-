import { useCallback, useEffect, useId, useState } from 'react'
import { useFingerprint } from '../hooks/useFingerprint'
import type { FingerprintComplete, FingerprintRequest, FingerprintSource } from '../lib/api'
import { isNum } from '../lib/chart'
import { formatDec, formatRa } from '../lib/coords'
import { copyText, downloadText, exportStem, fingerprintJson, pointsCsv } from '../lib/export'
import type { Target } from '../lib/viewState'
import { LightCurveGrid } from './charts/LightCurveGrid'
import { SpectrumChart } from './charts/SpectrumChart'
import { ClassificationCard } from './ClassificationCard'

interface Props {
  target: Target
  fov: number
  open: boolean
  onOpenChange: (open: boolean) => void
  /** From App: the season's start month ("2025-05"), or just the number when the field is not loaded. */
  seasonLabel: (season: number) => string
}

const BUTTON = 'btn btn-sm'

const fmt = (v: number | null | undefined, digits: number) => (isNum(v) ? v.toFixed(digits) : '–')

function SourcesTable({ sources, catalog }: { sources: FingerprintSource[]; catalog: FingerprintComplete['catalog'] }) {
  const ordered = [...sources].sort((a, b) => Number(b.role === 'target') - Number(a.role === 'target') || a.sep_arcsec - b.sep_arcsec)
  return (
    <div className="overflow-x-auto rounded border border-ink-800">
      <table className="w-full min-w-[32rem] text-left text-xs">
        <caption className="sr-only">Target and neighbouring sources used in the fit</caption>
        <thead className="bg-ink-900 text-ink-300">
          <tr>
            <th scope="col" className="px-2 py-1.5 font-semibold">Source</th>
            <th scope="col" className="px-2 py-1.5 font-semibold">Sep (″)</th>
            <th scope="col" className="px-2 py-1.5 font-semibold">G mag</th>
            <th scope="col" className="px-2 py-1.5 font-semibold">μα*, μδ (mas/yr)</th>
            <th scope="col" className="px-2 py-1.5 font-semibold">{catalog.name || 'Catalogue'} id</th>
          </tr>
        </thead>
        <tbody>
          {ordered.map((s) => (
            <tr key={s.id} className="border-t border-ink-800">
              <th scope="row" className="px-2 py-1 text-left font-normal">
                {s.role === 'target' ? (
                  <span className="font-semibold text-accent">Target</span>
                ) : (
                  <span className="text-slate-200">
                    {s.id}
                    {s.variable && (
                      <span className="ml-1.5 rounded border border-amber-400/40 bg-amber-400/15 px-1 py-px text-[11px] font-medium text-amber-200">
                        variable
                      </span>
                    )}
                  </span>
                )}
              </th>
              <td className="px-2 py-1 font-mono">{s.role === 'target' ? '0' : s.sep_arcsec.toFixed(1)}</td>
              <td className="px-2 py-1 font-mono">{fmt(s.g_mag, 2)}</td>
              <td className="px-2 py-1 font-mono">
                {isNum(s.pmra_masyr) && isNum(s.pmdec_masyr) ? `${s.pmra_masyr.toFixed(1)}, ${s.pmdec_masyr.toFixed(1)}` : '–'}
              </td>
              <td className="px-2 py-1 font-mono text-ink-300">{s.gaia_id ?? '–'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function MethodNote({ method }: { method: FingerprintComplete['method'] }) {
  const rows: [string, string][] = []
  if (method?.psf) rows.push(['PSF', method.psf])
  if (method?.background) rows.push(['Background', method.background])
  if (isNum(method?.fit_box_px)) rows.push(['Fit box', `${method.fit_box_px} px`])
  if (isNum(method?.neighbour_radius_arcsec)) rows.push(['Neighbour radius', `${method.neighbour_radius_arcsec}″`])
  const notes = method?.notes ?? []
  return (
    <details className="text-xs text-ink-300">
      <summary className="cursor-pointer select-none hover:text-slate-100">How this was measured</summary>
      <div className="mt-2 flex flex-col gap-2 rounded border border-ink-800 bg-ink-900 p-3">
        {rows.length > 0 && (
          <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1">
            {rows.map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-ink-500">{k}</dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
        )}
        {notes.length > 0 && (
          <ul className="list-disc pl-4">
            {notes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        )}
        <p>
          This is quick-look photometry from the cached cutouts, not a calibrated pipeline product. Negative flux values
          are noise scatter around a faint or undetected source, not negative brightness.
        </p>
      </div>
    </details>
  )
}

function Result({ data, target, seasonLabel }: { data: FingerprintComplete; target: Target; seasonLabel: (s: number) => string }) {
  const [copied, setCopied] = useState<'idle' | 'ok' | 'fail'>('idle')
  useEffect(() => {
    if (copied === 'idle') return
    const t = window.setTimeout(() => setCopied('idle'), 2200)
    return () => window.clearTimeout(t)
  }, [copied])

  const label = target.label || `${formatRa(target.ra)} ${formatDec(target.dec)}`
  const season = useCallback(
    (s: number) => {
      const l = seasonLabel(s)
      return l === String(s) ? `S${s}` : `S${s} · ${l}`
    },
    [seasonLabel],
  )

  const points = data.points ?? []
  const quality = { good: 0, suspect: 0, bad: 0 }
  for (const p of points) quality[p.quality] = (quality[p.quality] ?? 0) + 1
  const stem = exportStem(target.label, target.ra, target.dec)

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-ink-300">
          {points.length} exposure measurements ({quality.good} good
          {quality.suspect > 0 && `, ${quality.suspect} suspect`}
          {quality.bad > 0 && `, ${quality.bad} bad`}) · {data.spectrum?.length ?? 0} spectral channels
        </p>
        <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Export">
          <button
            type="button"
            className={BUTTON}
            onClick={() => downloadText(`${stem}.json`, fingerprintJson(data, window.location.href), 'application/json')}
          >
            Download JSON
          </button>
          <button type="button" className={BUTTON} onClick={() => downloadText(`${stem}.csv`, pointsCsv(points), 'text/csv')}>
            Download CSV
          </button>
          <button
            type="button"
            className={BUTTON}
            onClick={async () => setCopied((await copyText(window.location.href)) ? 'ok' : 'fail')}
          >
            Copy link
          </button>
          <span role="status" className="min-w-12 text-xs text-ink-300">
            {copied === 'ok' ? 'Copied' : copied === 'fail' ? 'Copy failed' : ''}
          </span>
        </div>
      </div>

      {data.blend_warning && (
        <div role="alert" className="flex gap-2 rounded-md border border-[rgb(242_184_75/0.35)] bg-[rgb(242_184_75/0.06)] p-3 text-[13px]">

          <div>
            <p className="font-semibold">Blended with a neighbouring source</p>
            <p className="text-ink-300">{data.blend_note}</p>
          </div>
        </div>
      )}
      {data.catalog && !data.catalog.ok && (
        <p className="text-xs text-ink-300">
          Neighbours could not be checked against {data.catalog.name || 'the catalogue'}
          {data.catalog.note ? ` (${data.catalog.note})` : ''}, so the deblended spectrum may still contain blended light.
        </p>
      )}

      {data.classification && <ClassificationCard classification={data.classification} />}

      <section className="flex flex-col gap-2" aria-label="Spectrum">
        <h3 className="text-[13px] font-semibold">Spectrum</h3>
        <SpectrumChart
          spectrum={data.spectrum ?? []}
          neighbourSpectra={data.neighbour_spectra ?? {}}
          sources={data.sources ?? []}
          seasonLabel={season}
          targetName={label}
        />
      </section>

      <section className="flex flex-col gap-2" aria-label="Light curves">
        <h3 className="text-[13px] font-semibold">Light curves</h3>
        <LightCurveGrid curves={data.light_curves ?? []} seasonLabel={season} />
      </section>

      <section className="flex flex-col gap-2" aria-label="Sources">
        <h3 className="text-[13px] font-semibold">Sources in the fit</h3>
        <SourcesTable sources={data.sources ?? []} catalog={data.catalog ?? { name: '', ok: true, note: null }} />
      </section>

      <MethodNote method={data.method ?? {}} />
    </div>
  )
}

function Body({ target, fov, seasonLabel }: Omit<Props, 'open' | 'onOpenChange'>) {
  const pm = target.pm
  const req: FingerprintRequest = { ra: target.ra, dec: target.dec, fov, pm }
  const { data, error, retry } = useFingerprint(req)

  if (error) {
    return (
      <div role="alert" className="flex flex-wrap items-center gap-3 rounded-md border border-red-900 bg-red-950/40 p-3 text-sm text-red-300">
        <span>Could not build the fingerprint: {error}</span>
        <button type="button" className={BUTTON} onClick={retry}>
          Retry
        </button>
      </div>
    )
  }
  if (!data) return <p className="text-sm text-ink-300">Contacting the server…</p>
  if (data.status === 'processing') {
    const pct = data.total > 0 ? Math.min(100, (data.done / data.total) * 100) : 3
    return (
      <div className="flex flex-col gap-1" role="status">
        <div className="flex justify-between gap-3 text-xs text-ink-300">
          <span>{data.stage || 'Working'}…</span>
          {data.total > 0 && (
            <span className="font-mono">
              {data.done}/{data.total}
            </span>
          )}
        </div>
        <div className="h-1.5 overflow-hidden rounded bg-ink-800">
          <div className="h-full bg-accent transition-all" style={{ width: `${pct}%` }} />
        </div>
        <p className="text-xs text-ink-500">
          Fitting every exposure with its neighbours. This takes a while the first time and is cached afterwards.
        </p>
      </div>
    )
  }
  if (data.status === 'error') return null // handled through `error` above
  return <Result data={data} target={target} seasonLabel={seasonLabel} />
}

export function FingerprintPanel({ target, fov, open, onOpenChange, seasonLabel }: Props) {
  const bodyId = useId()
  return (
    <section className="flex flex-col gap-4" aria-labelledby={`${bodyId}-title`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 id={`${bodyId}-title`} className="section-title">
            Spectrum and light curve
          </h2>
          <p className="section-lede">
            Fingerprint of the source: its 0.75–5 µm spectrum with neighbours fitted out, and how its flux changes
            between seasons.
          </p>
        </div>
        {!open && (
          <button type="button" aria-expanded={open} aria-controls={bodyId} onClick={() => onOpenChange(true)} className="btn btn-primary btn-sm">
            Measure the spectrum
          </button>
        )}
      </div>
      <div id={bodyId} hidden={!open}>
        {open && <Body target={target} fov={fov} seasonLabel={seasonLabel} />}
      </div>
    </section>
  )
}
