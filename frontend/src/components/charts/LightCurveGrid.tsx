import { useMemo, useState } from 'react'
import type { FingerprintLightCurve, FingerprintLightCurvePoint } from '../../lib/api'
import { clamp, fmtFluxErr, fmtTick, isNum, linear, niceTicks, weightedMean } from '../../lib/chart'
import { mjdToDate } from '../../lib/coords'
import { SERIES_COLOR } from './chartStyle'
import { Marker, MarkerKey } from './marks'

const W = 300
const H = 150
const M = { l: 44, r: 12, t: 10, b: 28 }
const MAX_SHOWN = 9

interface Props {
  curves: FingerprintLightCurve[]
  /** Short season label, e.g. "S1 · 2025-05". */
  seasonLabel: (season: number) => string
}

const minus = (s: string) => s.replace('-', '−')
const lamRange = (c: FingerprintLightCurve) => `${c.lam_lo_um.toFixed(2)}–${c.lam_hi_um.toFixed(2)} µm`
const redChi2 = (c: FingerprintLightCurve) => (isNum(c.chi2_const) && isNum(c.dof) && c.dof > 0 ? c.chi2_const / c.dof : null)

interface XAxis {
  scale: (mjd: number) => number
  /** one tick per distinct season, at the mean epoch of that season */
  ticks: { season: number; x: number }[]
}

function LightCurveCell({ curve, xAxis, seasonLabel }: { curve: FingerprintLightCurve; xAxis: XAxis; seasonLabel: (s: number) => string }) {
  const [hover, setHover] = useState<number | null>(null)
  const pts = useMemo(
    () => curve.points.filter((p): p is FingerprintLightCurvePoint & { flux_ujy: number } => isNum(p.flux_ujy)),
    [curve],
  )
  const chi = redChi2(curve)

  const { yScale, yTicks, mean } = useMemo(() => {
    const lo0 = Math.min(...pts.map((p) => p.flux_ujy - (isNum(p.flux_err_ujy) ? p.flux_err_ujy : 0)))
    const hi0 = Math.max(...pts.map((p) => p.flux_ujy + (isNum(p.flux_err_ujy) ? p.flux_err_ujy : 0)))
    const ok = pts.length > 0
    const pad = ok ? (hi0 - lo0) * 0.08 : 0
    const t = niceTicks(ok ? lo0 - pad : -1, ok ? hi0 + pad : 1, 3)
    const d: [number, number] = [t[0], t[t.length - 1]]
    return {
      yScale: linear(d[0], d[1], H - M.b, M.t),
      yTicks: t,
      mean: weightedMean(pts.filter((p) => isNum(p.flux_err_ujy)).map((p) => ({ v: p.flux_ujy, e: p.flux_err_ujy as number }))),
    }
  }, [pts])
  const step = yTicks.length > 1 ? yTicks[1] - yTicks[0] : undefined
  const yClamp = (y: number) => clamp(y, M.t, H - M.b)
  const hp = hover === null ? null : pts[hover]

  return (
    <figure
      className={`relative flex flex-col gap-1 rounded-md border p-2 ${curve.variable ? 'border-amber-400/40 bg-amber-400/[0.04]' : 'border-ink-800 bg-ink-900'}`}
    >
      <figcaption className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs">
        <span className="font-semibold text-slate-100">{lamRange(curve)}</span>
        <span className="text-ink-300">D{curve.detector}</span>
        {curve.variable && (
          <span className="rounded border border-amber-400/40 bg-amber-400/15 px-1.5 py-px text-[10px] font-medium text-amber-200">
            variable
          </span>
        )}
        <span className="ml-auto font-mono text-ink-300" title="Chi-squared of a constant-flux fit, per degree of freedom">
          {chi !== null ? `χ²/dof ${chi.toFixed(1)}` : 'χ²/dof –'}
          {isNum(curve.dof) ? ` (${curve.dof})` : ''}
        </span>
      </figcaption>

      <div className="relative">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          className="block h-auto w-full select-none"
          role="img"
          aria-label={`Light curve ${lamRange(curve)}, detector ${curve.detector}: ${pts.length} seasons. ${
            chi !== null ? `Chi-squared per degree of freedom ${chi.toFixed(1)} for a constant flux. ` : ''
          }${curve.variable ? 'Flagged as variable.' : 'Consistent with constant flux.'}`}
        >
          {yTicks.map((t) => (
            <g key={t}>
              <line x1={M.l} x2={W - M.r} y1={yScale(t)} y2={yScale(t)} className="stroke-ink-800" strokeWidth={1} />
              <text x={M.l - 5} y={yScale(t) + 3.5} textAnchor="end" className="fill-ink-300 text-[10px]">
                {minus(fmtTick(t, step))}
              </text>
            </g>
          ))}
          {yTicks[0] < 0 && yTicks[yTicks.length - 1] > 0 && (
            <line x1={M.l} x2={W - M.r} y1={yScale(0)} y2={yScale(0)} className="stroke-ink-500" strokeWidth={1} />
          )}
          {mean !== null && (
            <line
              x1={M.l}
              x2={W - M.r}
              y1={yClamp(yScale(mean))}
              y2={yClamp(yScale(mean))}
              stroke={SERIES_COLOR.deblended}
              strokeWidth={1}
              strokeDasharray="4 3"
              opacity={0.5}
            />
          )}
          <line x1={M.l} x2={W - M.r} y1={H - M.b} y2={H - M.b} className="stroke-ink-500" strokeWidth={1} />
          {xAxis.ticks.map((t) => (
            <g key={t.season}>
              <line x1={t.x} x2={t.x} y1={H - M.b} y2={H - M.b + 4} className="stroke-ink-500" strokeWidth={1} />
              <text x={t.x} y={H - M.b + 16} textAnchor="middle" className="fill-ink-300 text-[10px]">
                S{t.season}
              </text>
            </g>
          ))}
          <text transform={`translate(9 ${(M.t + H - M.b) / 2}) rotate(-90)`} textAnchor="middle" className="fill-ink-300 text-[10px]">
            Flux (µJy)
          </text>

          {pts.map((p, i) => {
            const x = xAxis.scale(p.mjd)
            const e = isNum(p.flux_err_ujy) ? p.flux_err_ujy : 0
            return (
              <g key={`${p.season}-${i}`}>
                <line x1={x} x2={x} y1={yClamp(yScale(p.flux_ujy - e))} y2={yClamp(yScale(p.flux_ujy + e))} stroke={SERIES_COLOR.deblended} strokeWidth={1.25} opacity={0.75} />
                <Marker shape="circle" x={x} y={yClamp(yScale(p.flux_ujy))} r={4} color={SERIES_COLOR.deblended} />
                {hover === i && <circle cx={x} cy={yClamp(yScale(p.flux_ujy))} r={7} fill="none" stroke="#f1f5f9" strokeWidth={1.25} />}
              </g>
            )
          })}
          {/* generous hit targets (24px) around each point */}
          {pts.map((p, i) => (
            <circle
              key={`hit-${p.season}-${i}`}
              cx={xAxis.scale(p.mjd)}
              cy={yClamp(yScale(p.flux_ujy))}
              r={14}
              fill="transparent"
              onPointerEnter={() => setHover(i)}
              onPointerDown={() => setHover(i)}
              onPointerLeave={(e) => {
                if (e.pointerType === 'mouse') setHover(null)
              }}
            />
          ))}
        </svg>
        {hp && (
          <div
            className="pointer-events-none absolute z-10 w-44 rounded border border-ink-700 bg-ink-950/95 px-2 py-1.5 text-[11px] leading-snug shadow-lg"
            style={{
              left: `${clamp((xAxis.scale(hp.mjd) / W) * 100, 28, 72)}%`,
              top: `${(yClamp(yScale(hp.flux_ujy)) / H) * 100}%`,
              transform: `translate(-50%, ${yClamp(yScale(hp.flux_ujy)) > H / 2 ? 'calc(-100% - 12px)' : '12px'})`,
            }}
          >
            <div className="font-semibold text-slate-100">{seasonLabel(hp.season)}</div>
            <div className="font-mono text-slate-100">{fmtFluxErr(hp.flux_ujy, hp.flux_err_ujy)} µJy</div>
            <div className="text-ink-300">
              {mjdToDate(hp.mjd).toISOString().slice(0, 10)} · n = {hp.n}
              {chi !== null && <> · χ²/dof {chi.toFixed(1)}</>}
            </div>
          </div>
        )}
      </div>
    </figure>
  )
}

export function LightCurveGrid({ curves, seasonLabel }: Props) {
  const [showAll, setShowAll] = useState(false)

  // Variable curves first (highest chi2 first), then the rest by wavelength.
  const sorted = useMemo(
    () =>
      [...curves].sort(
        (a, b) =>
          Number(b.variable) - Number(a.variable) ||
          (a.variable ? (redChi2(b) ?? 0) - (redChi2(a) ?? 0) : a.detector - b.detector || a.lam_lo_um - b.lam_lo_um),
      ),
    [curves],
  )
  const shown = showAll ? sorted : sorted.slice(0, MAX_SHOWN)
  const nVariable = curves.filter((c) => c.variable).length

  // One time axis for every panel so seasons line up between them.
  const xAxis = useMemo<XAxis>(() => {
    const bySeason = new Map<number, number[]>()
    for (const c of curves) for (const p of c.points) bySeason.set(p.season, [...(bySeason.get(p.season) ?? []), p.mjd])
    const all = [...bySeason.values()].flat()
    const lo = all.length ? Math.min(...all) : 0
    const hi = all.length ? Math.max(...all) : 1
    const pad = Math.max((hi - lo) * 0.08, 15)
    const scale = linear(lo - pad, hi + pad, M.l, W - M.r)
    const ticks = [...bySeason.entries()]
      .map(([season, m]) => ({ season, x: scale(m.reduce((a, b) => a + b, 0) / m.length) }))
      .sort((a, b) => a.season - b.season)
    return { scale, ticks }
  }, [curves])
  const seasons = xAxis.ticks.map((t) => t.season)

  if (!curves.length) {
    return <p className="rounded-md border border-ink-800 bg-ink-900 p-4 text-sm text-ink-300">No light curves were built (a wavelength bin needs measurements in at least two seasons).</p>
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2 text-xs text-ink-300">
        <span>
          {nVariable > 0 ? `${nVariable} of ${curves.length} wavelength bins flagged variable` : `${curves.length} wavelength bins, none flagged variable`}
          {!showAll && curves.length > MAX_SHOWN && ` · showing ${shown.length}`}
        </span>
        {curves.length > MAX_SHOWN && (
          <button
            type="button"
            aria-pressed={showAll}
            onClick={() => setShowAll((v) => !v)}
            className="rounded border border-ink-700 px-2 py-1 text-xs text-ink-300 hover:border-ink-500 hover:text-slate-100 focus-visible:outline-2 focus-visible:outline-accent"
          >
            {showAll ? `Show top ${MAX_SHOWN}` : `Show all ${curves.length}`}
          </button>
        )}
      </div>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-3">
        {shown.map((c) => (
          <LightCurveCell key={`${c.detector}-${c.bin}`} curve={c} xAxis={xAxis} seasonLabel={seasonLabel} />
        ))}
      </div>
      <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-500">
        <span className="flex items-center gap-1.5">
          <MarkerKey shape="circle" color={SERIES_COLOR.deblended} size={12} />
          deblended flux per season, 1σ
        </span>
        <span>Dashed line: weighted mean. Each panel has its own flux scale.</span>
        <span>{seasons.map((s) => seasonLabel(s)).join(' · ')}</span>
      </p>
    </div>
  )
}
