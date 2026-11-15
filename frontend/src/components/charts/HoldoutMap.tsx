import { useState } from 'react'
import { isNum } from '../../lib/chart'
import { detectorColor } from '../../lib/constants'
import {
  DEFAULT_PIXSCALE_ARCSEC,
  seasonName,
  signed,
  type HoldoutEllipse,
  type HoldoutFit,
  type HoldoutMeasurement,
  type HoldoutPrediction,
} from '../../lib/holdout'

// Plot space is arcsec on the sky: u = x * scale grows to the RIGHT (west), v = -y * scale grows
// DOWN (south), so north is up and east is left, like the sky viewer.

const W = 480
const H = 320
const MARGIN = 36
const PRED = '#c4b5fd'
const TRACK = '#cbd5e1'
const LABEL = '#a3adc4'
const HALO = '#0d111b'
const MIN_SPAN_ARCSEC = 3

interface Pt {
  u: number
  v: number
}

interface Props {
  /** Every training measurement; the ones with a position are drawn (hollow when not used in the fit). */
  train: HoldoutMeasurement[]
  fit: HoldoutFit
  prediction: HoldoutPrediction
  /** Measurements of the held-out season. Never drawn, and never used for the zoom, until `revealed`. */
  reveal: HoldoutMeasurement[]
  revealed: boolean
  /** Index of the held-out season. */
  holdoutSeason: number
  pixscaleArcsec?: number
}

const toPt = (x: number, y: number, s: number): Pt => ({ u: x * s, v: -y * s })

/** 1, 2 or 5 times a power of ten, not above x. */
function niceLength(x: number): number {
  const p = 10 ** Math.floor(Math.log10(x))
  const m = x / p
  return Number(((m >= 5 ? 5 : m >= 2 ? 2 : 1) * p).toPrecision(1))
}

/** SVG rotation (degrees) of an ellipse whose major axis points `pa` degrees east of north. */
const svgAngle = (paDeg: number) => {
  const pa = (paDeg * Math.PI) / 180
  return (Math.atan2(-Math.cos(pa), -Math.sin(pa)) * 180) / Math.PI
}

function ErrorEllipse({
  cx,
  cy,
  e,
  k,
  color,
  dashed,
  fillOpacity,
}: {
  cx: number
  cy: number
  e: HoldoutEllipse | null | undefined
  k: number
  color: string
  dashed?: boolean
  fillOpacity?: number
}) {
  if (!e || !isNum(e.a_arcsec) || !isNum(e.b_arcsec) || !isNum(e.pa_deg)) return null
  return (
    <ellipse
      cx={cx}
      cy={cy}
      rx={Math.max(e.a_arcsec * k, 0.5)}
      ry={Math.max(e.b_arcsec * k, 0.5)}
      transform={`rotate(${svgAngle(e.pa_deg)} ${cx} ${cy})`}
      fill={color}
      fillOpacity={fillOpacity ?? 0}
      stroke={color}
      strokeWidth={1.25}
      strokeDasharray={dashed ? '4 3' : undefined}
    />
  )
}

function Label({ x, y, children }: { x: number; y: number; children: string }) {
  return (
    <text
      x={x}
      y={y}
      textAnchor="middle"
      fontSize={11}
      fill={LABEL}
      stroke={HALO}
      strokeWidth={3}
      paintOrder="stroke"
      strokeLinejoin="round"
    >
      {children}
    </text>
  )
}

const Diamond = ({ cx, cy, color }: { cx: number; cy: number; color: string }) => (
  <path
    d={`M${cx} ${cy - 5.5} L${cx + 5.5} ${cy} L${cx} ${cy + 5.5} L${cx - 5.5} ${cy} Z`}
    fill={color}
    stroke="#f8fafc"
    strokeWidth={1.25}
  />
)

interface Drawn {
  m: HoldoutMeasurement
  p: Pt
}

function withPosition(list: HoldoutMeasurement[], s: number): Drawn[] {
  return list.flatMap((m) => (isNum(m.x) && isNum(m.y) ? [{ m, p: toPt(m.x, m.y, s) }] : []))
}

/**
 * Sky map of the prediction test: training positions with their error ellipses, the fitted
 * straight-line track, and the predicted position (dashed until the answer is revealed).
 * The map zooms to what is drawn, so nothing about the held-out result shows before `revealed`.
 */
export function HoldoutMap({ train, fit, prediction, reveal, revealed, holdoutSeason, pixscaleArcsec }: Props) {
  const s = pixscaleArcsec && pixscaleArcsec > 0 ? pixscaleArcsec : DEFAULT_PIXSCALE_ARCSEC
  const [scrubYr, setScrubYr] = useState<number>(() => prediction.extrapolation_yr)

  const trainPts = withPosition(train, s)
  const usedPts = trainPts.filter((d) => d.m.used_in_fit)
  const measured = revealed ? withPosition(reveal, s) : []
  const pred = toPt(prediction.x, prediction.y, s)
  const stat = revealed && fit.static_model && isNum(fit.static_model.x) ? toPt(fit.static_model.x, fit.static_model.y, s) : null

  // Fitted track: position at the fit's reference epoch plus the fitted motion (east = -u, north = -v).
  const at = (mjd: number): Pt => {
    const yr = (mjd - fit.ref_mjd) / 365.25
    return {
      u: fit.x * s - (fit.pmra_masyr / 1000) * yr,
      v: -fit.y * s - (fit.pmdec_masyr / 1000) * yr,
    }
  }
  const epochs = (usedPts.length ? usedPts : trainPts).flatMap((d) => (isNum(d.m.mjd_mean) ? [d.m.mjd_mean] : []))
  const tFirst = epochs.length ? Math.min(...epochs) : fit.ref_mjd
  const tLast = epochs.length ? Math.max(...epochs) : fit.ref_mjd
  const trackA = at(tFirst)
  const trackB = at(tLast)

  // Scrubbed position along proper motion vector
  const scrubPt = at(fit.ref_mjd + scrubYr * 365.25)

  // Zoom to everything drawn, ellipses and scrub point included.
  const extents: { p: Pt; r: number }[] = [
    ...trainPts.map((d) => ({ p: d.p, r: d.m.ellipse?.a_arcsec ?? 0 })),
    ...measured.map((d) => ({ p: d.p, r: d.m.ellipse?.a_arcsec ?? 0 })),
    { p: pred, r: prediction.ellipse.a_arcsec },
    { p: trackA, r: 0 },
    { p: trackB, r: 0 },
    { p: scrubPt, r: 0.5 },
    ...(stat ? [{ p: stat, r: 0 }] : []),
  ].filter((x) => isNum(x.p.u) && isNum(x.p.v))
  let minU = Infinity
  let maxU = -Infinity
  let minV = Infinity
  let maxV = -Infinity
  for (const { p, r } of extents) {
    minU = Math.min(minU, p.u - r)
    maxU = Math.max(maxU, p.u + r)
    minV = Math.min(minV, p.v - r)
    maxV = Math.max(maxV, p.v + r)
  }
  const spanU = Math.max(maxU - minU, MIN_SPAN_ARCSEC)
  const spanV = Math.max(maxV - minV, MIN_SPAN_ARCSEC)
  const k = Math.min((W - 2 * MARGIN) / spanU, (H - 2 * MARGIN) / spanV)
  const cu = (minU + maxU) / 2
  const cv = (minV + maxV) / 2
  const sx = (u: number) => W / 2 + (u - cu) * k
  const sy = (v: number) => H / 2 + (v - cv) * k

  const barArcsec = niceLength((W * 0.25) / k)
  const barPx = barArcsec * k

  const px = sx(pred.u)
  const py = sy(pred.v)
  const bx = sx(trackB.u)
  const by = sy(trackB.v)
  const ax = sx(trackA.u)
  const ay = sy(trackA.v)

  const scrubSx = sx(scrubPt.u)
  const scrubSy = sy(scrubPt.v)

  // Arrow head at the predicted end of the track.
  const dir = Math.atan2(py - by, px - bx)
  const hasArrow = Math.hypot(px - bx, py - by) > 6 || Math.hypot(px - ax, py - ay) > 6
  const headFrom = Math.hypot(px - bx, py - by) > 6 ? dir : Math.atan2(py - ay, px - ax)
  const head = (sign: 1 | -1) =>
    `${px - 9 * Math.cos(headFrom + sign * 0.45)},${py - 9 * Math.sin(headFrom + sign * 0.45)}`

  // One label per training season, above its topmost point.
  const bySeason = new Map<number, Drawn[]>()
  for (const d of trainPts) bySeason.set(d.m.season, [...(bySeason.get(d.m.season) ?? []), d])

  const detectors = [...new Set([...trainPts, ...measured].map((d) => d.m.detector))].sort((a, b) => a - b)
  const aria =
    `Sky map, east left and north up, in arcseconds. ${trainPts.length} training position${trainPts.length === 1 ? '' : 's'} ` +
    `from seasons ${[...bySeason.keys()].map((n) => n + 1).join(', ')}, the fitted straight-line track, and the predicted ` +
    `position for ${seasonName(holdoutSeason).toLowerCase()} with a ${prediction.ellipse.a_arcsec.toFixed(2)} by ` +
    `${prediction.ellipse.b_arcsec.toFixed(2)} arcsecond one-sigma error ellipse.` +
    (revealed ? ` ${measured.length} measured position${measured.length === 1 ? '' : 's'} of the held-out season shown.` : '')

  return (
    <figure className="flex flex-col gap-2">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={aria} className="h-auto w-full rounded border border-ink-700 bg-ink-950">
        {/* training positions */}
        {trainPts.map(({ m, p }) => {
          const cx = sx(p.u)
          const cy = sy(p.v)
          const color = detectorColor(m.detector)
          return (
            <g key={m.group} opacity={m.used_in_fit ? 1 : 0.6}>
              <ErrorEllipse cx={cx} cy={cy} e={m.ellipse} k={k} color={color} />
              <circle cx={cx} cy={cy} r={3.5} fill={m.used_in_fit ? color : 'none'} stroke={color} strokeWidth={1.5} />
            </g>
          )
        })}
        {[...bySeason.entries()].map(([season, pts]) => (
          <Label
            key={season}
            x={pts.reduce((a, d) => a + sx(d.p.u), 0) / pts.length}
            y={Math.min(...pts.map((d) => sy(d.p.v) - Math.max((d.m.ellipse?.a_arcsec ?? 0) * k, 4))) - 6}
          >
            {`S${season + 1}`}
          </Label>
        ))}

        {/* fitted track: solid across the training epochs, dashed on the way to the prediction */}
        <line x1={ax} y1={ay} x2={bx} y2={by} stroke={TRACK} strokeWidth={1.5} />
        <line x1={bx} y1={by} x2={px} y2={py} stroke={TRACK} strokeWidth={1.5} strokeDasharray="5 4" />
        {hasArrow && (
          <polyline
            points={`${head(1)} ${px},${py} ${head(-1)}`}
            fill="none"
            stroke={TRACK}
            strokeWidth={1.5}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
        )}

        {/* no-motion position: where the object would be if it had not moved */}
        {stat && (
          <g>
            <path
              d={`M${sx(stat.u) - 4} ${sy(stat.v) - 4} l8 8 m0 -8 l-8 8`}
              stroke="#8892ad"
              strokeWidth={1.5}
              fill="none"
            />
            <Label x={sx(stat.u)} y={sy(stat.v) + 18}>
              no motion
            </Label>
          </g>
        )}

        {/* prediction */}
        <ErrorEllipse cx={px} cy={py} e={prediction.ellipse} k={k} color={PRED} dashed={!revealed} fillOpacity={revealed ? 0.12 : 0.05} />
        <path d={`M${px - 5} ${py} h10 M${px} ${py - 5} v10`} stroke={PRED} strokeWidth={1.5} />
        <Label x={px} y={py + Math.max(prediction.ellipse.a_arcsec * k, 6) + 14}>
          {`Predicted S${holdoutSeason + 1}`}
        </Label>

        {/* Timeline scrubber: interactive extrapolated position & uncertainty */}
        {isNum(scrubSx) && isNum(scrubSy) && (
          <g className="transition-all duration-75">
            <circle cx={scrubSx} cy={scrubSy} r={4.5} fill="#e8ff00" stroke="#070a14" strokeWidth={1.5} />
            <circle cx={scrubSx} cy={scrubSy} r={7.5} fill="none" stroke="#e8ff00" strokeWidth={1} strokeOpacity={0.6} />
            <Label x={scrubSx} y={scrubSy - 8 - 4}>
              {`Scrub: ${scrubYr >= 0 ? '+' : ''}${scrubYr.toFixed(2)}y`}
            </Label>
          </g>
        )}

        {/* reveal: measured positions and how far each is from the prediction */}
        {measured.map(({ m, p }) => (
          <line key={`l-${m.group}`} x1={px} y1={py} x2={sx(p.u)} y2={sy(p.v)} stroke="#f8fafc" strokeOpacity={0.55} strokeWidth={1} />
        ))}
        {measured.map(({ m, p }) => (
          <g key={m.group}>
            <ErrorEllipse cx={sx(p.u)} cy={sy(p.v)} e={m.ellipse} k={k} color={detectorColor(m.detector)} />
            <Diamond cx={sx(p.u)} cy={sy(p.v)} color={detectorColor(m.detector)} />
          </g>
        ))}
        {measured.length > 0 && (
          <Label
            x={measured.reduce((a, d) => a + sx(d.p.u), 0) / measured.length}
            y={Math.min(...measured.map((d) => sy(d.p.v) - Math.max((d.m.ellipse?.a_arcsec ?? 0) * k, 4))) - 10}
          >
            {`Measured S${holdoutSeason + 1}`}
          </Label>
        )}

        {/* scale bar (bottom left) and compass (top right), both in the empty margin */}
        <g stroke={LABEL} strokeWidth={1.5} fill="none">
          <path d={`M12 ${H - 20} h${barPx} M12 ${H - 24} v8 M${12 + barPx} ${H - 24} v8`} />
        </g>
        <text x={12} y={H - 6} fontSize={11} fill={LABEL}>
          {`${barArcsec}″`}
        </text>
        <g stroke={LABEL} strokeWidth={1.5} fill="none">
          <path d={`M${W - 14} 30 v-18 M${W - 14} 30 h-18`} />
        </g>
        <text x={W - 14} y={10} fontSize={10} textAnchor="middle" fill={LABEL}>
          N
        </text>
        <text x={W - 40} y={34} fontSize={10} textAnchor="middle" fill={LABEL}>
          E
        </text>
      </svg>

      {/* Timeline Scrubber Controls */}
      <div className="flex flex-col gap-2 rounded-lg border border-ink-800 bg-ink-900/90 p-2.5 text-xs">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="font-semibold text-slate-200">Scrub the fitted motion</span>
          <div className="flex flex-wrap items-center gap-x-2 tabular text-[11px] text-ink-300">
            <span className="font-semibold text-slate-100">
              Δt = {scrubYr >= 0 ? '+' : ''}{scrubYr.toFixed(2)} yr
            </span>
            <span className="text-ink-500">|</span>
            <span>
              Offset: Δα* = {signed((fit.pmra_masyr / 1000) * scrubYr, 2)}″, Δδ = {signed((fit.pmdec_masyr / 1000) * scrubYr, 2)}″
            </span>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <input
            type="range"
            min={Math.min(-1.5, Math.floor(prediction.extrapolation_yr - 1.0))}
            max={Math.max(3.5, Math.ceil(prediction.extrapolation_yr + 1.5))}
            step={0.05}
            value={scrubYr}
            onChange={(e) => setScrubYr(parseFloat(e.target.value))}
            className="h-6 min-w-[8rem] flex-1 cursor-pointer"
            aria-label="Timeline proper motion scrubber in years"
          />
          <div className="flex items-center gap-1.5 shrink-0">
            <button
              type="button"
              onClick={() => setScrubYr(0)}
              className="btn btn-sm"
              title="Reset to reference epoch"
            >
              Fit epoch
            </button>
            <button
              type="button"
              onClick={() => setScrubYr(prediction.extrapolation_yr)}
              className="btn btn-sm"
              title="Jump to held-out season prediction"
            >
              Prediction ({prediction.extrapolation_yr >= 0 ? '+' : ''}{prediction.extrapolation_yr.toFixed(2)}y)
            </button>
          </div>
        </div>

        <p className="text-[11px] text-ink-400 leading-relaxed">
          Linear 2D sky-plane proper-motion extrapolation. This visualises constant tangent plane drift; it is not an orbital determination.
          Only the backend prediction carries an uncertainty ellipse; the scrubbed position is illustrative.
        </p>
      </div>

      <figcaption>
        <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-ink-300" aria-label="Map legend">
          {detectors.map((d) => (
            <li key={d} className="inline-flex items-center gap-1.5">
              <span aria-hidden className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: detectorColor(d) }} />
              {`Detector ${d}`}
            </li>
          ))}
          <li className="inline-flex items-center gap-1.5">
            <span aria-hidden className="h-2.5 w-2.5 rounded-full border border-ink-300" />
            Hollow: not used in the fit
          </li>
          <li className="inline-flex items-center gap-1.5">
            <span aria-hidden className="inline-block w-5 border-t-2" style={{ borderColor: TRACK }} />
            Fitted motion
          </li>
          <li className="inline-flex items-center gap-1.5">
            <span aria-hidden className="inline-block h-2.5 w-4 rounded-full border border-dashed" style={{ borderColor: PRED }} />
            Prediction (1σ)
          </li>
          <li className="inline-flex items-center gap-1.5">
            <span aria-hidden className="inline-block h-2.5 w-2.5 rounded-full bg-accent" />
            Scrubbed position
          </li>
          {revealed && (
            <li className="inline-flex items-center gap-1.5">
              <span aria-hidden className="inline-block h-2 w-2 rotate-45 border border-slate-50 bg-ink-500" />
              Measured (1σ)
            </li>
          )}
        </ul>
        <p className="mt-1 text-[11px] text-ink-500">Ellipses are 1σ position errors. East is left, north is up; the map zooms to the points.</p>
      </figcaption>
    </figure>
  )
}
