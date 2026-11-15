import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from 'react'
import { MOLECULAR_FEATURES, SPHEREX_BANDS } from '../../constants/spherex'
import { useElementWidth } from '../../hooks/useElementWidth'
import type { FingerprintChannel, FingerprintNeighbourChannel, FingerprintSource } from '../../lib/api'
import {
  clamp,
  fmtFlux,
  fmtFluxErr,
  fmtTick,
  isNum,
  linear,
  logScale,
  logTicks,
  median,
  niceTicks,
  type Scale,
} from '../../lib/chart'
import { DETECTORS } from '../../lib/constants'
import { SEASON_OVERFLOW, SEASON_STYLES, SERIES_COLOR, type MarkerShape } from './chartStyle'
import { ErrorBar, Marker, MarkerKey } from './marks'

const X_LO = 0.7
const X_HI = 5.1
const M = { l: 52, r: 12, t: 36, b: 48 }

type SeriesId = 'deblended' | 'naive' | 'neighbour'

interface Mark {
  key: string
  series: SeriesId
  channel: number
  x: number
  y: number
  yLo: number
  yHi: number
  shape: MarkerShape
  color: string
  hollow: boolean
  opacity: number
}

interface Props {
  spectrum: FingerprintChannel[]
  neighbourSpectra: Record<string, FingerprintNeighbourChannel[]>
  sources: FingerprintSource[]
  /** Short label for a season, e.g. "S1 · 2025-05". */
  seasonLabel: (season: number) => string
  targetName: string
}

const axisMinus = (s: string) => s.replace('-', '−')

function LegendToggle(props: { pressed: boolean; onClick: () => void; disabled?: boolean; children: ReactNode; title?: string }) {
  const { pressed, onClick, disabled, children, title } = props
  return (
    <button
      type="button"
      aria-pressed={pressed}
      disabled={disabled}
      title={title}
      onClick={onClick}
      className={`flex items-center gap-1.5 rounded border px-2 py-1 text-xs transition-colors focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-40 ${
        pressed ? 'border-ink-500 bg-ink-800 text-slate-100' : 'border-ink-800 text-ink-300 hover:border-ink-700'
      }`}
    >
      {children}
    </button>
  )
}

export function SpectrumChart({ spectrum, neighbourSpectra, sources, seasonLabel, targetName }: Props) {
  const [wrapRef, width] = useElementWidth<HTMLDivElement>()
  const clipId = useId()
  const [show, setShow] = useState({ deblended: true, naive: true, neighbour: false })
  const [bySeason, setBySeason] = useState(false)
  const [logY, setLogY] = useState(false)
  const [active, setActive] = useState<number | null>(null)
  const [isPlayingSonification, setIsPlayingSonification] = useState(false)
  const audioCtxRef = useRef<AudioContext | null>(null)
  const sonificationTimeoutRef = useRef<number | null>(null)

  const stopSonification = () => {
    if (audioCtxRef.current) {
      try {
        audioCtxRef.current.close()
      } catch {
        // Ignored
      }
      audioCtxRef.current = null
    }
    if (sonificationTimeoutRef.current) {
      window.clearTimeout(sonificationTimeoutRef.current)
      sonificationTimeoutRef.current = null
    }
    setIsPlayingSonification(false)
  }

  useEffect(() => {
    return () => {
      stopSonification()
    }
  }, [])

  const rows = useMemo(
    () => spectrum.filter((r) => isNum(r.wavelength_um)).sort((a, b) => a.wavelength_um - b.wavelength_um),
    [spectrum],
  )

  const playSonification = () => {
    const validRows = rows.filter(
      (r): r is typeof r & { flux_ujy: number } => typeof r.flux_ujy === 'number' && Number.isFinite(r.flux_ujy),
    )
    if (validRows.length === 0) return

    stopSonification()

    const AudioContextClass =
      window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
    if (!AudioContextClass) return

    const ctx = new AudioContextClass()
    audioCtxRef.current = ctx
    setIsPlayingSonification(true)

    const fluxes = validRows.map((r) => r.flux_ujy)
    const minFlux = Math.min(...fluxes)
    const maxFlux = Math.max(...fluxes)
    const fluxSpan = Math.max(0.01, maxFlux - minFlux)

    const totalDurationSec = 4.0
    const stepDuration = totalDurationSec / validRows.length

    validRows.forEach((r, i) => {
      const startTime = ctx.currentTime + i * stepDuration
      const norm = Math.max(0, Math.min(1, (r.flux_ujy - minFlux) / fluxSpan))
      // Map flux to pitch between 220 Hz (A3) and 1050 Hz (C6)
      const freq = 220 + norm * 830

      const osc = ctx.createOscillator()
      const gain = ctx.createGain()

      osc.type = 'triangle'
      osc.frequency.setValueAtTime(freq, startTime)

      gain.gain.setValueAtTime(0, startTime)
      gain.gain.linearRampToValueAtTime(0.08, startTime + stepDuration * 0.15)
      gain.gain.exponentialRampToValueAtTime(0.001, startTime + stepDuration * 0.95)

      osc.connect(gain)
      gain.connect(ctx.destination)

      osc.start(startTime)
      osc.stop(startTime + stepDuration)
    })

    sonificationTimeoutRef.current = window.setTimeout(() => {
      void ctx.close().catch(() => {})
      setIsPlayingSonification(false)
      audioCtxRef.current = null
    }, (totalDurationSec + 0.1) * 1000)
  }

  const toggleSonification = () => {
    if (isPlayingSonification) {
      stopSonification()
    } else {
      playSonification()
    }
  }
  const seasons = useMemo(() => {
    const s = new Set<number>()
    for (const r of rows) for (const k of Object.keys(r.seasons ?? {})) s.add(Number(k))
    return [...s].filter(Number.isFinite).sort((a, b) => a - b)
  }, [rows])
  const neighbours = useMemo(
    () => Object.entries(neighbourSpectra ?? {}).filter(([, v]) => v?.length),
    [neighbourSpectra],
  )
  const neighbourLookup = useMemo(
    () => new Map(neighbours.map(([id, v]) => [id, new Map(v.map((c) => [c.channel, c]))])),
    [neighbours],
  )
  const hasSeasons = seasons.length > 0
  const hasNeighbours = neighbours.length > 0
  const seasonMode = bySeason && hasSeasons && show.deblended
  const seasonStyle = (s: number) => SEASON_STYLES[seasons.indexOf(s)] ?? SEASON_OVERFLOW
  const sourceById = useMemo(() => new Map(sources.map((s) => [s.id, s])), [sources])

  // ---- geometry ----
  const w = Math.max(280, width)
  const h = Math.round(clamp(w * 0.5, 280, 400))
  const pw = w - M.l - M.r
  const ph = h - M.t - M.b
  const xs = useMemo(() => linear(X_LO, X_HI, M.l, M.l + pw), [pw])
  const pitch = pw / Math.max(rows.length, 1)
  const r = clamp(pitch * 0.42, 1.6, 3.2)

  const showNeighbours = show.neighbour && hasNeighbours
  const nSeries = Number(show.deblended) + Number(show.naive) + Number(showNeighbours)
  const gap = Math.min(3.5, pitch * 0.4)

  // ---- y domain from whatever is visible ----
  const { yScale, yTicks, yDomain, skipped } = useMemo(() => {
    const vals: { f: number; e: number }[] = []
    const add = (f: number | null, e: number | null) => {
      if (isNum(f)) vals.push({ f, e: isNum(e) ? Math.abs(e) : 0 })
    }
    if (show.deblended) {
      for (const row of rows) {
        if (seasonMode) for (const s of Object.values(row.seasons ?? {})) add(s.flux_ujy, s.flux_err_ujy)
        else add(row.flux_ujy, row.flux_err_ujy)
      }
    }
    if (show.naive) for (const row of rows) add(row.aperture_flux_ujy, row.aperture_err_ujy)
    if (showNeighbours) for (const [, v] of neighbours) for (const c of v) add(c.flux_ujy, c.flux_err_ujy)

    const usable = logY ? vals.filter((v) => v.f > 0) : vals
    let skipped = vals.length - usable.length
    if (!usable.length) {
      const d: [number, number] = logY ? [1, 10] : [-1, 1]
      const t = logY ? logTicks(1, 10) : niceTicks(-1, 1, 4)
      const make = logY ? logScale : linear
      return { yScale: make(d[0], d[1], M.t + ph, M.t) as Scale, yTicks: t, yDomain: d, skipped }
    }
    const medErr = median(usable.map((v) => v.e).filter((e) => e > 0)) || 0
    // Typical error bars shape the range; a few huge ones are clipped instead of squashing the plot.
    const cap = medErr > 0 ? medErr * 2 : 0
    if (logY) {
      // A few near-zero fluxes would stretch the axis over many empty decades, so the floor follows the
      // lower few percent of the data; anything below it is left off (and counted in the caption).
      const sortedF = usable.map((v) => v.f).sort((a, b) => a - b)
      const minPos = sortedF[Math.floor(sortedF.length * 0.03)]
      const maxPos = Math.max(...usable.map((v) => v.f + Math.min(v.e, cap)))
      const lo = 10 ** Math.floor(Math.log10(minPos * 0.8))
      const hi = 10 ** Math.ceil(Math.log10(maxPos * 1.25))
      const d: [number, number] = [lo, hi]
      skipped += usable.filter((v) => v.f < lo).length
      return { yScale: logScale(lo, hi, M.t + ph, M.t), yTicks: logTicks(lo, hi), yDomain: d, skipped }
    }
    let lo = Math.min(...usable.map((v) => v.f - Math.min(v.e, cap)))
    let hi = Math.max(...usable.map((v) => v.f + Math.min(v.e, cap)))
    if (lo > 0 && lo < hi * 0.5) lo = 0
    if (hi < 0 && hi > lo * 0.5) hi = 0
    const pad = (hi - lo) * 0.04
    const t = niceTicks(lo - pad, hi + pad, 5)
    const d: [number, number] = [t[0], t[t.length - 1]]
    return { yScale: linear(d[0], d[1], M.t + ph, M.t), yTicks: t, yDomain: d, skipped }
  }, [rows, neighbours, show, showNeighbours, seasonMode, logY, ph])

  const yStep = yTicks.length > 1 ? yTicks[1] - yTicks[0] : undefined
  const yBottom = M.t + ph

  // ---- marks ----
  const marks = useMemo(() => {
    const Y = (v: number) => clamp(yScale(logY ? Math.max(v, yDomain[0]) : v), M.t, M.t + ph)
    const out: Mark[] = []
    const push = (m: Omit<Mark, 'yLo' | 'yHi' | 'y'> & { f: number; e: number | null }) => {
      if (logY && m.f < yDomain[0]) return
      const e = isNum(m.e) ? Math.abs(m.e) : 0
      const { f, ...rest } = m
      out.push({ ...rest, y: Y(f), yLo: Y(f - e), yHi: Y(f + e) })
    }
    const off = (slot: number) => (nSeries > 1 ? (slot - (nSeries - 1) / 2) * gap : 0)
    // Draw order = z order: neighbours (back), naive, deblended (front). Slots keep them side by side.
    let slot = 0
    const deblendedSlot = show.deblended ? slot++ : -1
    const naiveSlot = show.naive ? slot++ : -1
    const neighbourSlot = showNeighbours ? slot++ : -1

    if (showNeighbours) {
      const dx = off(neighbourSlot)
      for (const [id, v] of neighbours) {
        for (const c of v) {
          if (!isNum(c.flux_ujy) || !isNum(c.wavelength_um)) continue
          push({
            key: `n-${id}-${c.channel}`, series: 'neighbour', channel: c.channel, x: xs(c.wavelength_um) + dx,
            shape: 'diamond', color: SERIES_COLOR.neighbour, hollow: false, opacity: 0.55, f: c.flux_ujy, e: c.flux_err_ujy,
          })
        }
      }
    }
    if (show.naive) {
      const dx = off(naiveSlot)
      for (const row of rows) {
        if (!isNum(row.aperture_flux_ujy)) continue
        push({
          key: `a-${row.channel}`, series: 'naive', channel: row.channel, x: xs(row.wavelength_um) + dx, shape: 'square',
          color: seasonMode ? SERIES_COLOR.naiveNeutral : SERIES_COLOR.naive, hollow: true, opacity: 0.95,
          f: row.aperture_flux_ujy, e: row.aperture_err_ujy,
        })
      }
    }
    if (show.deblended) {
      const dx = off(deblendedSlot)
      const subGap = Math.min(2.2, pitch * 0.3)
      for (const row of rows) {
        if (seasonMode) {
          for (const [k, sv] of Object.entries(row.seasons ?? {})) {
            if (!isNum(sv.flux_ujy)) continue
            const idx = seasons.indexOf(Number(k))
            const st = SEASON_STYLES[idx] ?? SEASON_OVERFLOW
            push({
              key: `s${k}-${row.channel}`, series: 'deblended', channel: row.channel,
              x: xs(row.wavelength_um) + dx + (idx - (seasons.length - 1) / 2) * subGap,
              shape: st.shape, color: st.color, hollow: false, opacity: 0.95, f: sv.flux_ujy, e: sv.flux_err_ujy,
            })
          }
        } else if (isNum(row.flux_ujy)) {
          push({
            key: `d-${row.channel}`, series: 'deblended', channel: row.channel, x: xs(row.wavelength_um) + dx, shape: 'circle',
            color: SERIES_COLOR.deblended, hollow: false, opacity: 0.95, f: row.flux_ujy, e: row.flux_err_ujy,
          })
        }
      }
    }
    return out
  }, [rows, neighbours, show, showNeighbours, seasonMode, seasons, logY, xs, yScale, yDomain, pitch, nSeries, gap, ph])

  const deblendedMarks = useMemo(
    () => marks.filter((m) => m.series === 'deblended').sort((a, b) => a.x - b.x),
    [marks],
  )
  const deblendedRibbonPath = useMemo(() => {
    if (deblendedMarks.length < 2) return null
    const topPoints = deblendedMarks.map((m) => `${m.x},${m.yLo}`)
    const bottomPoints = [...deblendedMarks].reverse().map((m) => `${m.x},${m.yHi}`)
    return `M${topPoints[0]} L${topPoints.slice(1).join(' L')} L${bottomPoints.join(' L')} Z`
  }, [deblendedMarks])

  const [moleculeFilter, setMoleculeFilter] = useState<'all' | 'H₂O' | 'CH₄' | 'CO' | 'none'>('all')
  const [hoveredFeatureId, setHoveredFeatureId] = useState<string | null>(null)

  const visibleFeatures = useMemo(() => {
    if (moleculeFilter === 'none') return []
    if (moleculeFilter === 'all') return MOLECULAR_FEATURES
    return MOLECULAR_FEATURES.filter((f) => f.molecule === moleculeFilter)
  }, [moleculeFilter])

  // ---- interaction ----
  const rowX = useMemo(() => rows.map((row) => xs(row.wavelength_um)), [rows, xs])
  const nearest = (px: number) => {
    let best = 0
    let bd = Infinity
    for (let i = 0; i < rowX.length; i++) {
      const d = Math.abs(rowX[i] - px)
      if (d < bd) {
        bd = d
        best = i
      }
    }
    return best
  }
  const onMove = (e: PointerEvent<SVGRectElement>) => {
    if (!rows.length) return
    const box = e.currentTarget.getBoundingClientRect()
    setActive(nearest(((e.clientX - box.left) / box.width) * pw + M.l))
  }
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!rows.length) return
    const last = rows.length - 1
    if (e.key === 'ArrowRight') setActive((a) => (a === null ? 0 : Math.min(last, a + 1)))
    else if (e.key === 'ArrowLeft') setActive((a) => (a === null ? last : Math.max(0, a - 1)))
    else if (e.key === 'Home') setActive(0)
    else if (e.key === 'End') setActive(last)
    else if (e.key === 'Escape') setActive(null)
    else return
    e.preventDefault()
  }

  const activeIdx = active === null ? null : Math.min(active, rows.length - 1)
  const activeRow = activeIdx === null ? null : rows[activeIdx]
  const activeX = activeIdx === null ? 0 : rowX[activeIdx]
  const tipW = 228
  const tipLeft = activeX + 14 + tipW < w ? activeX + 14 : Math.max(0, activeX - 14 - tipW)

  const summary = useMemo(() => {
    const dm = median(rows.map((x) => x.flux_ujy).filter(isNum))
    const am = median(rows.map((x) => x.aperture_flux_ujy).filter(isNum))
    const first = rows[0]?.wavelength_um.toFixed(2) ?? '–'
    const last = rows[rows.length - 1]?.wavelength_um.toFixed(2) ?? '–'
    const bits = [`Flux versus wavelength for ${targetName}: ${rows.length} spectral channels from ${first} to ${last} micrometres.`]
    if (Number.isFinite(dm)) bits.push(`Median deblended flux ${fmtFlux(dm)} microjansky.`)
    if (Number.isFinite(am)) bits.push(`Median naive aperture flux ${fmtFlux(am)} microjansky.`)
    return bits.join(' ')
  }, [rows, targetName])

  const live = activeRow
    ? `${activeRow.wavelength_um.toFixed(3)} micrometres, detector ${activeRow.detector}. Deblended ${fmtFluxErr(activeRow.flux_ujy, activeRow.flux_err_ujy)} microjansky. Naive aperture ${fmtFluxErr(activeRow.aperture_flux_ujy, activeRow.aperture_err_ujy)} microjansky. ${activeRow.n} exposures.`
    : ''

  if (!rows.length) {
    return (
      <p className="rounded-md border border-ink-800 bg-ink-900 p-4 text-sm text-ink-300">
        No spectral channels were measured for this position.
      </p>
    )
  }

  const detBounds = DETECTORS.slice(1).map((d, i) => (DETECTORS[i].hi + d.lo) / 2)
  const naiveColor = seasonMode ? SERIES_COLOR.naiveNeutral : SERIES_COLOR.naive

  return (
    <div className="flex flex-col gap-2">
      {/* The legend doubles as the series toggles. */}
      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Spectrum series and scale">
        <LegendToggle pressed={show.deblended} onClick={() => setShow((s) => ({ ...s, deblended: !s.deblended }))}>
          <MarkerKey shape="circle" color={SERIES_COLOR.deblended} />
          Deblended (PSF fit with neighbours)
        </LegendToggle>
        <LegendToggle pressed={show.naive} onClick={() => setShow((s) => ({ ...s, naive: !s.naive }))}>
          <MarkerKey shape="square" color={naiveColor} hollow />
          Naive aperture
        </LegendToggle>
        <LegendToggle
          pressed={showNeighbours}
          disabled={!hasNeighbours}
          title={hasNeighbours ? undefined : 'No neighbour spectra were fitted'}
          onClick={() => setShow((s) => ({ ...s, neighbour: !s.neighbour }))}
        >
          <MarkerKey shape="diamond" color={SERIES_COLOR.neighbour} />
          Neighbour spectra{hasNeighbours ? ` (${neighbours.length})` : ''}
        </LegendToggle>
        <span className="mx-1 hidden h-5 w-px bg-ink-700 sm:block" aria-hidden="true" />
        <LegendToggle
          pressed={seasonMode}
          disabled={!hasSeasons || !show.deblended}
          title={hasSeasons ? 'Colour the deblended points by observing season' : 'No per-season fluxes available'}
          onClick={() => setBySeason((v) => !v)}
        >
          Colour by season
        </LegendToggle>
        <div className="flex" role="group" aria-label="Flux axis scale">
          {(['Linear', 'Log'] as const).map((label) => {
            const on = (label === 'Log') === logY
            return (
              <button
                key={label}
                type="button"
                className={`rounded border border-ink-700 px-2 py-0.5 text-xs transition-colors first:rounded-r-none last:rounded-l-none ${
                  on ? 'border-accent bg-accent/20 text-accent font-semibold' : 'text-ink-400 hover:text-white'
                }`}
                onClick={() => setLogY(label === 'Log')}
              >
                {label}
              </button>
            )
          })}
        </div>

        {/* Sonification Toggle (Web Audio API) */}
        <button
          type="button"
          onClick={toggleSonification}
          className={`flex items-center gap-1.5 rounded border px-2.5 py-1 text-xs transition-colors focus-visible:outline-2 focus-visible:outline-accent ${
            isPlayingSonification
              ? 'border-amber-400 bg-amber-500/20 text-amber-200 font-bold'
              : 'border-ink-700 text-ink-300 hover:border-ink-500 hover:text-slate-100'
          }`}
          title="Web Audio API Sonification: map 0.75-5.0 µm flux to pitch"
          aria-label={isPlayingSonification ? 'Stop audio sonification' : 'Play spectral flux sonification'}
        >
          <span aria-hidden="true">{isPlayingSonification ? '⏹' : '🔊'}</span>
          <span>{isPlayingSonification ? 'Stop Audio' : 'Sonify'}</span>
        </button>

        {/* Molecular absorption features filter */}
        <div className="ml-auto flex items-center gap-1.5 text-xs">
          <span className="text-ink-400 font-medium hidden sm:inline">Absorption markers:</span>
          {(['all', 'H₂O', 'CH₄', 'CO', 'none'] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => setMoleculeFilter(m)}
              className={`rounded px-2 py-0.5 text-[11px] font-medium transition-all ${
                moleculeFilter === m
                  ? 'border border-line bg-[#e8ff00]/15 text-[#e8ff00] font-bold'
                  : 'text-ink-400 hover:bg-ink-800 hover:text-slate-200'
              }`}
              title={m === 'all' ? 'Show all molecular bands' : m === 'none' ? 'Hide all markers' : `Show ${m} bands`}
            >
              {m}
            </button>
          ))}
        </div>
      </div>
      {seasonMode && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-300" role="group" aria-label="Season key">
          {seasons.map((s) => (
            <span key={s} className="flex items-center gap-1.5">
              <MarkerKey shape={seasonStyle(s).shape} color={seasonStyle(s).color} />
              {seasonLabel(s)}
            </span>
          ))}
        </div>
      )}

      <div
        ref={wrapRef}
        tabIndex={0}
        role="group"
        aria-label="Spectrum chart. Use the left and right arrow keys to read each channel."
        onKeyDown={onKey}
        onBlur={() => setActive(null)}
        className="relative rounded outline-none focus-visible:outline-2 focus-visible:outline-accent"
      >
        <svg viewBox={`0 0 ${w} ${h}`} className="block h-auto w-full touch-pan-y select-none" role="img" aria-label={summary}>
          <defs>
            <clipPath id={clipId}>
              <rect x={M.l} y={M.t} width={pw} height={ph} />
            </clipPath>
          </defs>

          {/* SPHEREx detector background band shading */}
          {SPHEREX_BANDS.map((b) => {
            const x0 = xs(Math.max(X_LO, b.minWavelengthUm))
            const x1 = xs(Math.min(X_HI, b.maxWavelengthUm))
            return (
              <rect
                key={`band-bg-${b.id}`}
                x={x0}
                y={M.t}
                width={Math.max(0, x1 - x0)}
                height={ph}
                fill={b.color}
                opacity={0.045}
              />
            )
          })}

          {/* Molecular absorption bands: peer-reviewed cited features */}
          {visibleFeatures.map((b, i) => {
            const x0 = xs(Math.max(X_LO, b.minWavelengthUm))
            const x1 = xs(Math.min(X_HI, b.maxWavelengthUm))
            const row = b.molecule === 'CH₄' ? 1 : 0
            const ly = 13 + row * 13
            const isHovered = hoveredFeatureId === b.id

            return (
              <g
                key={b.id || i}
                className="cursor-help"
                onMouseEnter={() => setHoveredFeatureId(b.id)}
                onMouseLeave={() => setHoveredFeatureId(null)}
              >
                <rect
                  x={x0}
                  y={M.t}
                  width={Math.max(0, x1 - x0)}
                  height={ph}
                  className={isHovered ? 'fill-[#e8ff00]/15' : 'fill-ink-300/[0.08]'}
                />
                <path
                  d={`M${x0},${ly + 3} V${ly} H${x1} V${ly + 3}`}
                  fill="none"
                  stroke={isHovered ? '#e8ff00' : '#8899bf'}
                  strokeWidth={isHovered ? 1.5 : 1}
                />
                <text
                  x={(x0 + x1) / 2}
                  y={ly - 2}
                  textAnchor="middle"
                  className={`text-[10px] font-medium ${isHovered ? 'fill-[#e8ff00] font-bold' : 'fill-ink-300'}`}
                >
                  {b.molecule}
                </text>
              </g>
            )
          })}

          {/* Detector boundaries (subtle) with labels under the axis. */}
          {detBounds.map((b, i) => (
            <line key={i} x1={xs(b)} x2={xs(b)} y1={M.t} y2={yBottom + 18} className="stroke-ink-700" strokeWidth={1} />
          ))}
          {DETECTORS.map((d) => (
            <text key={d.id} x={xs((d.lo + d.hi) / 2)} y={yBottom + 29} textAnchor="middle" className="fill-ink-300 text-[10px]" opacity={0.75}>
              D{d.id}
            </text>
          ))}

          {/* y axis */}
          {yTicks.map((t) => (
            <g key={t}>
              <line x1={M.l} x2={M.l + pw} y1={yScale(t)} y2={yScale(t)} className="stroke-ink-800" strokeWidth={1} />
              <text x={M.l - 6} y={yScale(t) + 3.5} textAnchor="end" className="fill-ink-300 text-[11px]">
                {axisMinus(logY ? fmtTick(t) : fmtTick(t, yStep))}
              </text>
            </g>
          ))}
          {!logY && yDomain[0] < 0 && yDomain[1] > 0 && (
            <line x1={M.l} x2={M.l + pw} y1={yScale(0)} y2={yScale(0)} className="stroke-ink-500" strokeWidth={1} />
          )}
          <text transform={`translate(12 ${M.t + ph / 2}) rotate(-90)`} textAnchor="middle" className="fill-ink-300 text-[11px]">
            Flux ({logY ? 'log ' : ''}µJy)
          </text>

          {/* x axis */}
          <line x1={M.l} x2={M.l + pw} y1={yBottom} y2={yBottom} className="stroke-ink-500" strokeWidth={1} />
          {[1, 2, 3, 4, 5].map((t) => (
            <g key={t}>
              <line x1={xs(t)} x2={xs(t)} y1={yBottom} y2={yBottom + 4} className="stroke-ink-500" strokeWidth={1} />
              <text x={xs(t)} y={yBottom + 16} textAnchor="middle" className="fill-ink-300 text-[11px]">
                {t}
              </text>
            </g>
          ))}
          <text x={M.l + pw / 2} y={h - 4} textAnchor="middle" className="fill-ink-300 text-[11px]">
            Wavelength (µm)
          </text>

          {/* data */}
          <g clipPath={`url(#${clipId})`}>
            {/* Shaded 1-sigma uncertainty ribbon for deblended curve */}
            {show.deblended && deblendedRibbonPath && (
              <path
                d={deblendedRibbonPath}
                fill="rgba(56, 189, 248, 0.14)"
                stroke="rgba(56, 189, 248, 0.3)"
                strokeWidth={0.75}
              />
            )}
            {activeRow && (
              <line x1={activeX} x2={activeX} y1={M.t} y2={yBottom} className="stroke-ink-300" strokeWidth={1} opacity={0.6} />
            )}
            {marks.map((m) => (
              <ErrorBar key={`e-${m.key}`} x={m.x} y0={m.yLo} y1={m.yHi} color={m.color} opacity={m.series === 'neighbour' ? 0.35 : 0.65} />
            ))}
            {marks.map((m) => (
              <Marker key={m.key} shape={m.shape} x={m.x} y={m.y} r={r} color={m.color} hollow={m.hollow} opacity={m.opacity} />
            ))}
            {activeRow &&
              marks
                .filter((m) => m.channel === activeRow.channel)
                .map((m) => <circle key={`ring-${m.key}`} cx={m.x} cy={m.y} r={r + 3.5} fill="none" stroke="#f1f5f9" strokeWidth={1.25} />)}
          </g>

          {/* hit layer: nearest channel, so the pointer only has to be close */}
          <rect
            x={M.l}
            y={M.t}
            width={pw}
            height={ph}
            fill="transparent"
            onPointerDown={onMove}
            onPointerMove={onMove}
            onPointerLeave={(e) => {
              if (e.pointerType === 'mouse') setActive(null)
            }}
          />
        </svg>

        {activeRow && (
          <div
            className="pointer-events-none absolute z-10 rounded border border-ink-700 bg-ink-950/95 px-2.5 py-2 text-[11px] leading-snug shadow-lg"
            style={{ left: tipLeft, top: M.t + 4, width: tipW }}
          >
            <div className="mb-1 flex items-baseline justify-between gap-2">
              <span className="font-mono text-xs font-semibold text-slate-100">{activeRow.wavelength_um.toFixed(3)} µm</span>
              <span className="text-ink-300">
                D{activeRow.detector} · ch {activeRow.channel}
              </span>
            </div>
            <ul className="flex flex-col gap-0.5">
              {show.deblended && (
                <li className="flex items-center gap-1.5">
                  <MarkerKey shape="circle" color={SERIES_COLOR.deblended} size={12} />
                  <span className="text-ink-300">Deblended</span>
                  <span className="ml-auto font-mono text-slate-100">{fmtFluxErr(activeRow.flux_ujy, activeRow.flux_err_ujy)}</span>
                </li>
              )}
              {seasonMode &&
                seasons.map((s) => {
                  const sv = activeRow.seasons?.[String(s)]
                  if (!sv) return null
                  return (
                    <li key={s} className="flex items-center gap-1.5 pl-2">
                      <MarkerKey shape={seasonStyle(s).shape} color={seasonStyle(s).color} size={12} />
                      <span className="text-ink-300">{seasonLabel(s)}</span>
                      <span className="ml-auto font-mono text-slate-100">
                        {fmtFluxErr(sv.flux_ujy, sv.flux_err_ujy)}
                        <span className="text-ink-500"> n{sv.n}</span>
                      </span>
                    </li>
                  )
                })}
              {show.naive && (
                <li className="flex items-center gap-1.5">
                  <MarkerKey shape="square" color={naiveColor} hollow size={12} />
                  <span className="text-ink-300">Naive aperture</span>
                  <span className="ml-auto font-mono text-slate-100">
                    {fmtFluxErr(activeRow.aperture_flux_ujy, activeRow.aperture_err_ujy)}
                  </span>
                </li>
              )}
              {showNeighbours &&
                neighbours.map(([id]) => {
                  const c = neighbourLookup.get(id)?.get(activeRow.channel)
                  const g = sourceById.get(id)?.g_mag
                  return (
                    <li key={id} className="flex items-center gap-1.5">
                      <MarkerKey shape="diamond" color={SERIES_COLOR.neighbour} size={12} />
                      <span className="text-ink-300">
                        {id}
                        {isNum(g) ? ` · G ${g.toFixed(1)}` : ''}
                      </span>
                      <span className="ml-auto font-mono text-slate-100">{c ? fmtFluxErr(c.flux_ujy, c.flux_err_ujy) : '–'}</span>
                    </li>
                  )
                })}
            </ul>
            <div className="mt-1 text-ink-300">
              µJy · {activeRow.n} exposure{activeRow.n === 1 ? '' : 's'}
            </div>
          </div>
        )}

        {/* Molecular absorption feature detail popover */}
        {hoveredFeatureId && (() => {
          const feat = MOLECULAR_FEATURES.find((f) => f.id === hoveredFeatureId)
          if (!feat) return null
          const fx = xs(feat.centerWavelengthUm)
          const leftPos = Math.max(8, Math.min(w - 290, fx - 145))
          return (
            <div
              className="pointer-events-none absolute z-20 rounded-md border border-line bg-ink-950/95 p-3 text-xs max-w-xs"
              style={{ left: leftPos, top: 2 }}
            >
              <div className="font-heading font-bold text-white flex items-center justify-between gap-2">
                <span className="text-slate-100 font-bold">{feat.label}</span>
                <span className="text-[#e8ff00] font-mono text-[10px] bg-[#e8ff00]/10 px-1.5 py-0.5 rounded">
                  {feat.minWavelengthUm.toFixed(2)}–{feat.maxWavelengthUm.toFixed(2)} µm
                </span>
              </div>
              <p className="text-ink-300 mt-1 leading-relaxed text-[11px]">{feat.description}</p>
              <div className="text-ink-500 mt-1.5 text-[10px] italic border-t border-ink-800 pt-1">
                Literature: {feat.citation}
              </div>
            </div>
          )
        })()}
        <p className="sr-only" aria-live="polite">
          {live}
        </p>
      </div>

      <p className="text-xs text-ink-500">
        Error bars are 1σ. Shaded bands mark approximate H₂O, CH₄ and CO absorption regions; D1–D6 are the detectors.
        {logY && skipped > 0 && ` Log scale: ${skipped} measurement${skipped === 1 ? '' : 's'} with flux ≤ 0 or below the axis not shown.`}
        {!logY && ' Fluxes below zero are noise scatter and are kept.'}
      </p>

      <details className="text-xs text-ink-300">
        <summary className="cursor-pointer select-none text-ink-300 hover:text-slate-100">Spectrum as a table</summary>
        <div className="mt-2 max-h-64 overflow-auto rounded border border-ink-800">
          <table className="w-full min-w-[34rem] text-left font-mono">
            <thead className="sticky top-0 bg-ink-900 text-ink-300">
              <tr>
                <th className="px-2 py-1 font-semibold">λ (µm)</th>
                <th className="px-2 py-1 font-semibold">Det</th>
                <th className="px-2 py-1 font-semibold">Deblended (µJy)</th>
                <th className="px-2 py-1 font-semibold">Naive (µJy)</th>
                {neighbours.map(([id]) => (
                  <th key={id} className="px-2 py-1 font-semibold">
                    {id} (µJy)
                  </th>
                ))}
                <th className="px-2 py-1 font-semibold">n</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.channel} className="border-t border-ink-800">
                  <td className="px-2 py-0.5">{row.wavelength_um.toFixed(3)}</td>
                  <td className="px-2 py-0.5">{row.detector}</td>
                  <td className="px-2 py-0.5">{fmtFluxErr(row.flux_ujy, row.flux_err_ujy)}</td>
                  <td className="px-2 py-0.5">{fmtFluxErr(row.aperture_flux_ujy, row.aperture_err_ujy)}</td>
                  {neighbours.map(([id]) => {
                    const c = neighbourLookup.get(id)?.get(row.channel)
                    return (
                      <td key={id} className="px-2 py-0.5">
                        {c ? fmtFluxErr(c.flux_ujy, c.flux_err_ujy) : '–'}
                      </td>
                    )
                  })}
                  <td className="px-2 py-0.5">{row.n}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  )
}
