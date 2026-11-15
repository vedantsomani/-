import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { useHoldout } from '../hooks/useHoldout'
import { isNum } from '../lib/chart'
import { detectorColor } from '../lib/constants'
import { formatDec, formatRa } from '../lib/coords'
import {
  holdoutQuery,
  isTestable,
  joinAnd,
  monthYear,
  seasonName,
  signed,
  type HoldoutComplete,
  type HoldoutComparison,
  type HoldoutMeasurement,
  type HoldoutModel,
  type HoldoutProcessing,
  type HoldoutSeason,
  type HoldoutTarget,
  type HoldoutTestable,
} from '../lib/holdout'
import { HoldoutMap } from './charts/HoldoutMap'

const BUTTON = 'btn btn-sm'
const PRIMARY = 'btn btn-primary'
const REVEAL = 'btn btn-primary min-h-10 px-5 text-[14px]'

interface Props {
  target: HoldoutTarget
  /** Field of view, arcmin: the same field as the viewer. */
  fov: number
}

const VERDICT_STYLE: Record<string, { label: string; glyph: string; cls: string }> = {
  'prediction confirmed': { label: 'Prediction confirmed', glyph: '✓', cls: 'border-green-500/50 bg-green-500/10 text-green-300' },
  marginal: { label: 'Marginal', glyph: '~', cls: 'border-amber-400/50 bg-amber-400/10 text-amber-200' },
  'prediction failed': { label: 'Prediction failed', glyph: '✗', cls: 'border-red-500/50 bg-red-500/10 text-red-300' },
}

const num = (v: number | null | undefined, digits: number) => (isNum(v) ? v.toFixed(digits) : '–')

function Step({ n, title, children, headingRef }: { n: number; title: string; children: ReactNode; headingRef?: React.Ref<HTMLHeadingElement> }) {
  return (
    <li className="flex gap-3">
      <span
        aria-hidden
        className="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full border border-ink-600 text-[11px] font-semibold text-ink-300"
      >
        {n}
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <h3 ref={headingRef} tabIndex={-1} className="text-sm font-semibold text-slate-100 outline-none">
          {title}
        </h3>
        {children}
      </div>
    </li>
  )
}

function DetectorDot({ d }: { d: number }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span aria-hidden className="h-2 w-2 rounded-full" style={{ backgroundColor: detectorColor(d) }} />
      {`D${d}`}
    </span>
  )
}

function TrainTable({ rows, seasons }: { rows: HoldoutMeasurement[]; seasons: HoldoutSeason[] }) {
  const date = (m: HoldoutMeasurement) => {
    const mjd = m.mjd_mean ?? seasons.find((s) => s.season === m.season)?.mjd_mean
    return isNum(mjd) ? monthYear(mjd) : ''
  }
  return (
    <div className="overflow-x-auto rounded border border-ink-800">
      <table className="w-full min-w-[26rem] text-left text-xs">
        <caption className="sr-only">Positions measured in the training seasons</caption>
        <thead className="bg-ink-900 text-ink-300">
          <tr>
            <th scope="col" className="px-2 py-1.5 font-semibold">Season</th>
            <th scope="col" className="px-2 py-1.5 font-semibold">Detector</th>
            <th scope="col" className="px-2 py-1.5 font-semibold">S/N</th>
            <th scope="col" className="px-2 py-1.5 font-semibold">Error E, N (″)</th>
            <th scope="col" className="px-2 py-1.5 font-semibold">Used in fit</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((m) => (
            <tr key={m.group} className="border-t border-ink-800">
              <th scope="row" className="px-2 py-1 text-left font-normal text-slate-200">
                {seasonName(m.season)}
                <span className="ml-1.5 text-ink-500">{date(m)}</span>
              </th>
              <td className="px-2 py-1"><DetectorDot d={m.detector} /></td>
              <td className="px-2 py-1 font-mono">{num(m.snr, 1)}</td>
              <td className="px-2 py-1 font-mono">
                {isNum(m.sigma_east_arcsec) && isNum(m.sigma_north_arcsec)
                  ? `${m.sigma_east_arcsec.toFixed(2)}, ${m.sigma_north_arcsec.toFixed(2)}`
                  : '–'}
              </td>
              <td className="px-2 py-1">
                {m.used_in_fit ? (
                  <span className="text-green-300">yes</span>
                ) : (
                  <span className="text-ink-500">no{m.reason || m.status ? `: ${m.reason || m.status}` : ''}</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function MotionLine({ label, value, sigma }: { label: string; value: number; sigma?: number | null }) {
  return (
    <div className="flex items-baseline gap-2">
      <dt className="w-12 text-ink-500">{label}</dt>
      <dd className="font-mono text-slate-100">
        {signed(value / 1000, 3)}
        {isNum(sigma) && <span className="text-ink-300">{` ± ${(sigma / 1000).toFixed(3)}`}</span>}
        <span className="text-ink-500"> ″/yr</span>
      </dd>
    </div>
  )
}

function Verdict({ verdict }: { verdict: string }) {
  const v = VERDICT_STYLE[verdict] ?? { label: verdict ? verdict[0]!.toUpperCase() + verdict.slice(1) : 'No verdict', glyph: '?', cls: 'border-ink-600 bg-ink-800 text-ink-300' }
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-sm font-semibold ${v.cls}`}>
      <span aria-hidden>{v.glyph}</span>
      {v.label}
    </span>
  )
}

const plxSourceText = (s: string | null | undefined) => (s === 'gaia' ? 'Gaia DR3' : s === 'query' ? 'supplied' : 'unknown')

function ModelTable({ linear, parallax }: { linear: HoldoutModel; parallax: HoldoutModel }) {
  const rows: [string, HoldoutModel][] = [
    ['Straight line', linear],
    [`With parallax (${num(parallax.parallax_mas, 1)} mas)`, parallax],
  ]
  return (
    <div className="overflow-x-auto rounded border border-ink-800">
      <table className="w-full min-w-[20rem] text-left text-xs">
        <caption className="sr-only">Held-out offset under each motion model</caption>
        <thead className="bg-ink-900 text-ink-300">
          <tr>
            <th scope="col" className="px-2 py-1.5 font-semibold">Model</th>
            <th scope="col" className="px-2 py-1.5 font-semibold">Offset (″)</th>
            <th scope="col" className="px-2 py-1.5 font-semibold">Sigma</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, m]) => (
            <tr key={label} className="border-t border-ink-800">
              <th scope="row" className="px-2 py-1 text-left font-normal text-slate-200">{label}</th>
              <td className="px-2 py-1 font-mono">{num(m.comparison?.combined?.offset_arcsec, 2)}</td>
              <td className="px-2 py-1 font-mono">{num(m.comparison?.combined?.offset_sigma, 1)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function RevealResult({ data, comparison }: { data: HoldoutTestable; comparison: HoldoutComparison }) {
  const c = comparison.combined
  const unmeasured = (data.reveal ?? []).filter((m) => !isNum(m.ra))
  const plx = data.parallax
  const failedFast = comparison.verdict !== 'prediction confirmed' && comparison.model !== 'linear+parallax' && data.fit.pm_total_masyr > 1000
  return (
    <div className="flex flex-col gap-2.5" aria-live="polite">
      <div className="flex flex-wrap items-center gap-2">
        <Verdict verdict={comparison.verdict} />
        <span className="text-[11px] text-ink-500">Confirmed within 2 sigma, marginal up to 3 sigma, failed beyond.</span>
      </div>

      {c ? (
        <>
          <p className="text-sm leading-relaxed text-slate-200">
            Found <span className="font-mono font-semibold text-slate-100">{c.offset_arcsec.toFixed(2)}″</span> from the prediction (
            {Math.abs(c.offset_east_arcsec).toFixed(2)}″ {c.offset_east_arcsec < 0 ? 'west' : 'east'},{' '}
            {Math.abs(c.offset_north_arcsec).toFixed(2)}″ {c.offset_north_arcsec < 0 ? 'south' : 'north'}), which is{' '}
            <span className="font-mono font-semibold text-slate-100">{c.offset_sigma.toFixed(1)} sigma</span> given the errors.
          </p>
          <p className="border-l-2 border-accent pl-3 text-sm leading-relaxed text-slate-200">
            A source that did not move would be off by{' '}
            <span className="font-mono font-semibold text-slate-100">{c.no_motion.offset_sigma.toFixed(1)} sigma</span> (
            {c.no_motion.offset_arcsec.toFixed(2)}″ from where it was found).
            {comparison.no_motion_verdict && <span className="text-ink-300">{` ${comparison.no_motion_verdict[0]!.toUpperCase()}${comparison.no_motion_verdict.slice(1)}.`}</span>}
          </p>
        </>
      ) : (
        comparison.summary && <p className="text-sm leading-relaxed text-slate-200">{comparison.summary}</p>
      )}

      {failedFast && (
        <p role="note" className="rounded-md border border-amber-400/40 bg-amber-400/5 p-2.5 text-xs leading-relaxed text-amber-100">
          This object is nearby, so its position also wobbles yearly with Earth&apos;s orbit (parallax). The straight-line model leaves
          that out
          {plx && isNum(plx.implied_mas) && isNum(plx.implied_err_mas)
            ? `: the held-out offset alone corresponds to a parallax of ${plx.implied_mas.toFixed(0)} ± ${plx.implied_err_mas.toFixed(0)} mas (implied by the reveal, not a fit).`
            : ', which can explain a miss of a few tenths of an arcsecond.'}
        </p>
      )}

      {data.models?.parallax && (
        <>
          <ModelTable linear={data.models.linear} parallax={data.models.parallax} />
          {plx && isNum(plx.implied_mas) && isNum(plx.implied_err_mas) && (
            <p className="text-[11px] text-ink-500">
              {`Parallax implied by the reveal, not a fit: ${plx.implied_mas.toFixed(0)} ± ${plx.implied_err_mas.toFixed(0)} mas (the value that would make the straight-line miss vanish; it uses the held-out season, so it does not enter the verdict).`}
            </p>
          )}
        </>
      )}

      {comparison.per_detector.length > 0 && (
        <div className="overflow-x-auto rounded border border-ink-800">
          <table className="w-full min-w-[24rem] text-left text-xs">
            <caption className="sr-only">Measured position against the prediction, per detector</caption>
            <thead className="bg-ink-900 text-ink-300">
              <tr>
                <th scope="col" className="px-2 py-1.5 font-semibold">Detector</th>
                <th scope="col" className="px-2 py-1.5 font-semibold">Offset (″)</th>
                <th scope="col" className="px-2 py-1.5 font-semibold">E, N (″)</th>
                <th scope="col" className="px-2 py-1.5 font-semibold">Sigma</th>
                <th scope="col" className="px-2 py-1.5 font-semibold">If it had not moved</th>
              </tr>
            </thead>
            <tbody>
              {comparison.per_detector.map((r) => (
                <tr key={r.detector} className="border-t border-ink-800">
                  <th scope="row" className="px-2 py-1 text-left font-normal text-slate-200"><DetectorDot d={r.detector} /></th>
                  <td className="px-2 py-1 font-mono">{r.offset_arcsec.toFixed(2)}</td>
                  <td className="px-2 py-1 font-mono">{`${signed(r.offset_east_arcsec, 2)}, ${signed(r.offset_north_arcsec, 2)}`}</td>
                  <td className="px-2 py-1 font-mono">{r.offset_sigma.toFixed(1)}</td>
                  <td className="px-2 py-1 font-mono">{`${r.no_motion.offset_sigma.toFixed(1)} sigma`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {unmeasured.length > 0 && (
        <p className="text-[11px] text-ink-500">
          Not measured in the held-out season:{' '}
          {unmeasured.map((m) => `D${m.detector}${m.reason || m.status ? ` (${m.reason || m.status})` : ''}`).join('; ')}.
        </p>
      )}
      {c && comparison.summary && <p className="text-xs leading-relaxed text-ink-300">{comparison.summary}</p>}
    </div>
  )
}

function Notes({ data }: { data: HoldoutComplete }) {
  const notes = data.notes ?? []
  const method = Object.entries(data.method ?? {})
  if (notes.length === 0 && method.length === 0) return null
  return (
    <details className="text-xs text-ink-300">
      <summary className="cursor-pointer rounded text-ink-300 hover:text-slate-100 focus-visible:outline-2 focus-visible:outline-accent">
        {`Notes and method${notes.length ? ` (${notes.length} note${notes.length === 1 ? '' : 's'})` : ''}`}
      </summary>
      {notes.length > 0 && (
        <ul className="mt-1.5 list-disc space-y-1 pl-5 leading-relaxed">
          {notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
      {method.length > 0 && (
        <dl className="mt-2 space-y-1.5 leading-relaxed">
          {method.map(([k, v]) => (
            <div key={k}>
              <dt className="font-semibold text-slate-200 capitalize">{k.replace(/_/g, ' ')}</dt>
              <dd className="text-ink-500">{v}</dd>
            </div>
          ))}
        </dl>
      )}
    </details>
  )
}

function Guided({
  data,
  revealed,
  onReveal,
  onHide,
}: {
  data: HoldoutTestable
  revealed: boolean
  onReveal: () => void
  onHide: () => void
}) {
  const { fit, prediction, comparison } = data
  const hold = data.holdout_season
  const seasons = data.seasons ?? []
  const train = data.train ?? []
  const trainSeasons = fit.seasons ?? seasons.filter((s) => s.role === 'train').map((s) => s.season)
  const date = (season: number) => {
    const m = seasons.find((s) => s.season === season)?.mjd_mean
    return isNum(m) ? monthYear(m) : null
  }
  const revealHeading = useRef<HTMLHeadingElement>(null)
  useEffect(() => {
    if (revealed) revealHeading.current?.focus()
  }, [revealed])

  const e = prediction.ellipse
  const withPlx = comparison.model === 'linear+parallax' && isNum(data.parallax?.used_mas)
  return (
    <ol className="flex flex-col gap-4">
      <Step n={1} title="Train">
        <p className="text-sm leading-relaxed text-slate-200">
          {`We fit a straight-line motion${withPlx ? ` plus the yearly parallax wobble (parallax fixed at ${data.parallax!.used_mas!.toFixed(1)} mas, ${plxSourceText(data.parallax!.source)}; not fitted)` : ''} using only ${trainSeasons.length === 1 ? 'season' : 'seasons'} ${joinAnd(trainSeasons.map((s) => String(s + 1)))}. `}
          {`${seasonName(hold)} was set aside and not looked at.`}
        </p>
        <p className="text-[11px] text-ink-500">
          {withPlx
            ? 'Model used for the verdict: straight line + parallax. The straight-line result is shown next to it after the reveal.'
            : 'Model used for the verdict: straight line (no parallax value available).'}
        </p>
        <dl className="flex flex-col gap-0.5 text-xs" aria-label="Fitted motion">
          <MotionLine label="East" value={fit.pmra_masyr} sigma={fit.sigma_pmra_masyr} />
          <MotionLine label="North" value={fit.pmdec_masyr} sigma={fit.sigma_pmdec_masyr} />
          <div className="flex items-baseline gap-2 text-ink-500">
            <dt className="w-12">Total</dt>
            <dd className="font-mono text-ink-300">
              {`${(fit.pm_total_masyr / 1000).toFixed(3)} ″/yr`}
              {isNum(fit.chi2_red) && <span className="text-ink-500">{`, fit quality χ²/dof ${fit.chi2_red.toFixed(2)}`}</span>}
            </dd>
          </div>
        </dl>
        {train.length > 0 && <TrainTable rows={train} seasons={seasons} />}
      </Step>

      <Step n={2} title="Predict">
        <p className="text-sm leading-relaxed text-slate-200">
          {`For ${seasonName(hold)}${date(hold) ? ` (${date(hold)})` : ''} this motion puts the source at `}
          <span className="font-mono text-slate-100">{`${formatRa(prediction.ra)} ${formatDec(prediction.dec)}`}</span>
          {`, inside a 1σ error ellipse of ${e.a_arcsec.toFixed(2)}″ × ${e.b_arcsec.toFixed(2)}″. `}
          <span className="text-ink-300">{`That is ${Math.abs(prediction.extrapolation_yr).toFixed(2)} yr from the fit's reference epoch.`}</span>
        </p>
        <HoldoutMap
          train={train}
          fit={fit}
          prediction={prediction}
          reveal={data.reveal ?? []}
          revealed={revealed}
          holdoutSeason={hold}
          pixscaleArcsec={data.grid?.pixscale_arcsec}
        />
      </Step>

      <Step n={3} title={revealed ? 'Reveal' : 'Reveal the held-out observation'} headingRef={revealHeading}>
        {revealed ? (
          <>
            <RevealResult data={data} comparison={comparison} />
            <div>
              <button type="button" className={BUTTON} onClick={onHide}>
                Hide the answer again
              </button>
            </div>
          </>
        ) : (
          <div className="flex flex-col items-start gap-2 rounded-md border border-ink-700 bg-ink-950 p-3">
            <p className="text-xs leading-relaxed text-ink-300">
              {`${seasonName(hold)} has not been looked at yet. The prediction above is fixed. Press the button to measure where the source really is.`}
            </p>
            <button type="button" className={REVEAL} onClick={onReveal}>
              Reveal the held-out observation
            </button>
          </div>
        )}
      </Step>
    </ol>
  )
}

function Progress({ p }: { p: HoldoutProcessing }) {
  const stage = p.stage ? p.stage.replace(/_/g, ' ') : ''
  const counts = isNum(p.total) && p.total > 0 && isNum(p.done) ? ` (${p.done}/${p.total})` : ''
  return (
    <p role="status" className="text-xs text-ink-300">
      {`Measuring the training seasons and fitting the motion${stage ? `: ${stage}${counts}` : ''}. This can take a minute.`}
    </p>
  )
}

/**
 * "Predict, then reveal": fit a straight-line motion to every season except one, predict where
 * the source must be in the held-out season, show that prediction, and only after the user asks,
 * show where it really was. Nothing is requested until "Run the prediction test" is pressed.
 */
export function PredictRevealPanel({ target, fov }: Props) {
  const titleId = useId()
  const query = holdoutQuery({ ra: target.ra, dec: target.dec, fov, pm: target.pm })
  const [run, setRun] = useState<{ query: string; revealed: boolean } | null>(null)
  // A different target or field forgets the run: the panel is collapsed again.
  const active = run !== null && run.query === query
  const revealed = run !== null && run.query === query && run.revealed
  const { data, error, retry } = useHoldout(active ? query : null)

  const complete = data?.status === 'complete' ? data : null

  return (
    <section className="text-[13px]" aria-labelledby={`${titleId}-title`}>
      <h2 id={`${titleId}-title`} className="section-title">
        Predict, then reveal
      </h2>

      {!active && (
        <div className="mt-2 flex flex-col items-start gap-2">
          <button type="button" className={PRIMARY} onClick={() => setRun({ query, revealed: false })}>
            Run the prediction test
          </button>
          <p className="text-xs text-ink-500">
            Set one season aside, fit a motion to the others, predict where the source must be, and only then look. A real mover
            lands where it was predicted; a fake one does not.
          </p>
        </div>
      )}

      {active && (
        <div className="mt-2 flex flex-col gap-3" aria-live="polite" aria-busy={!complete && !error}>
          {error && (
            <div
              role="alert"
              className="flex flex-wrap items-center gap-2 rounded-md border border-red-900 bg-red-950/40 p-2 text-xs text-red-300"
            >
              <span>Could not run the prediction test: {error}</span>
              <button type="button" className={BUTTON} onClick={retry}>
                Retry
              </button>
            </div>
          )}
          {!error && !complete && (data?.status === 'processing' ? <Progress p={data} /> : (
            <p role="status" className="text-xs text-ink-300">
              Starting the prediction test…
            </p>
          ))}
          {complete && !isTestable(complete) && (
            <p role="status" className="rounded-md border border-ink-700 bg-ink-950 p-2.5 text-sm leading-relaxed text-slate-200">
              {complete.reason || 'This source cannot be tested: there is not enough data to predict from.'}
            </p>
          )}
          {complete && isTestable(complete) && (
            <Guided
              data={complete}
              revealed={revealed}
              onReveal={() => setRun({ query, revealed: true })}
              onHide={() => setRun({ query, revealed: false })}
            />
          )}
          {complete && <Notes data={complete} />}
        </div>
      )}
    </section>
  )
}
