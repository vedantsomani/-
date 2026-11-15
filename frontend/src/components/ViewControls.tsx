import type { KeyboardEvent, ReactNode } from 'react'
import type { FieldResponse, RenderOpts } from '../lib/api'
import { effectiveScale, usesSigma } from '../lib/api'
import { BLINK_PANEL_ID, DETECTORS, viewTabId } from '../lib/constants'
import { MODE_TABS } from '../lib/modes'
import type { OverlayState } from '../hooks/useOverlay'
import type { Sequence } from '../lib/sequence'
import type { ViewState } from '../lib/viewState'
import { IconSliders } from './icons'
import { Popover } from './Popover'

interface Props {
  view: ViewState
  field: FieldResponse
  seq: Sequence
  seasonLabel: (s: number) => string
  update: (patch: Partial<ViewState>) => void
  /** Catalogue overlay status, shown next to the catalogue toggle. */
  overlay?: OverlayState
}

const toggled = (list: number[], v: number) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v].sort())

/** "2025-05-05" -> "May 2025" */
const monthYear = (iso: string) => {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso.slice(0, 7) : d.toLocaleDateString('en-GB', { month: 'short', year: 'numeric', timeZone: 'UTC' })
}
/** "2025-05" -> "May 25" */
const shortSeason = (label: string) => {
  const m = /^(\d{4})-(\d{2})/.exec(label)
  if (!m) return label
  return `${['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][Number(m[2]) - 1]} ’${m[1].slice(2)}`
}

/** Arrow-key navigation for a group of mutually exclusive buttons (tabs, radios). */
function rovingKeys<T>(e: KeyboardEvent<HTMLElement>, values: T[], current: T, select: (v: T) => void) {
  const i = values.indexOf(current)
  let next = -1
  if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = (i + 1) % values.length
  else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = (i - 1 + values.length) % values.length
  else if (e.key === 'Home') next = 0
  else if (e.key === 'End') next = values.length - 1
  if (next < 0) return
  e.preventDefault() // also tells the blink player's global key handler to leave this alone
  select(values[next])
  const buttons = e.currentTarget.parentElement?.querySelectorAll<HTMLElement>(':scope > button')
  buttons?.[next]?.focus()
}

const SIGMA_HELP =
  'σ is the typical noise level of the image; 6σ means a pixel differs by six times the noise, which is very unlikely to be chance.'

const Range = ({ label, title, ...p }: { label: string; title?: string; min: number; max: number; step: number; value: number; onChange: (v: number) => void }) => (
  <label className="flex flex-col gap-1 text-xs text-ink-300" title={title}>
    <span className="tabular">{label}</span>
    <input type="range" min={p.min} max={p.max} step={p.step} value={p.value} onChange={(e) => p.onChange(Number(e.target.value))} className="h-6" />
  </label>
)

const Check = ({ checked, onChange, children, title }: { checked: boolean; onChange: (v: boolean) => void; children: ReactNode; title?: string }) => (
  <label className="flex min-h-8 items-start gap-2 text-xs text-ink-300" title={title}>
    <input type="checkbox" className="mt-0.5 h-4 w-4 shrink-0" checked={checked} onChange={(e) => onChange(e.target.checked)} />
    <span>{children}</span>
  </label>
)

/** The one toolbar attached to the image: what to compare (mode, detector, seasons) and what to draw on top. */
export function ViewControls({ view, field, seq, seasonLabel, update, overlay }: Props) {
  const stacks = field.stacks ?? []
  const kind = view.mode === 'frames' ? 'frame' : 'stack'
  const frameCounts: Record<number, number> = {}
  field.frames.forEach((f) => (frameCounts[f.detector] = (frameCounts[f.detector] ?? 0) + 1))
  const setRender = (patch: Partial<RenderOpts>) => update({ render: { ...view.render, ...patch } })
  const rgb = view.mode === 'rgb'
  const diff = view.mode === 'diff'
  const sigma = rgb || usesSigma(view.render.scale, kind)
  const eff = effectiveScale(view.render.scale, kind)
  const singleDet = view.mode !== 'frames'
  const modes = MODE_TABS.map((t) => t.mode)
  const activeMode = MODE_TABS.find((t) => t.mode === view.mode)!
  const detIds = DETECTORS.map((d) => d.id)
  const showBins = (view.mode === 'bins' || (diff && !view.diffCombined)) && seq.bins.length > 0
  const gaiaNote =
    view.gaia && overlay
      ? overlay.failed
        ? 'Gaia unreachable'
        : overlay.loading
          ? 'Querying…'
          : overlay.data?.catwise && !overlay.data.catwise.ok
            ? 'CatWISE unreachable'
            : ''
      : ''
  const wiseNote =
    view.wise && overlay
      ? overlay.loading
        ? 'Querying…'
        : overlay.data?.wise && overlay.data.wise.catalog.ok === false
          ? 'AllWISE unreachable'
          : ''
      : ''

  return (
    <div className="stage-toolbar" role="toolbar" aria-label="Image controls">
      <div className="toolbar-row">
        <div className="seg" role="tablist" aria-label="Image type" data-tour="modes">
          {MODE_TABS.map((t) => {
            const on = view.mode === t.mode
            return (
              <button
                key={t.mode}
                id={viewTabId(t.mode)}
                role="tab"
                type="button"
                title={t.help}
                aria-selected={on}
                aria-controls={BLINK_PANEL_ID}
                tabIndex={on ? 0 : -1}
                onClick={() => update({ mode: t.mode })}
                onKeyDown={(e) => rovingKeys(e, modes, view.mode, (m) => update({ mode: m }))}
              >
                {t.label}
              </button>
            )
          })}
        </div>

        <div className="toolbar-group" data-tour="detectors">
          {rgb ? (
            <span className="help">D1–D6 combined</span>
          ) : singleDet ? (
            <div className="seg" role="radiogroup" aria-label="Detector">
              {DETECTORS.map((d) => {
                const on = view.det === d.id
                const n = stacks.filter((s) => s.detector === d.id && s.bin === null && s.visit == null).length
                return (
                  <button
                    key={d.id}
                    role="radio"
                    type="button"
                    className="det-chip inline-flex items-center"
                    aria-checked={on}
                    tabIndex={on ? 0 : -1}
                    title={`Detector ${d.id}: ${d.band} (${d.hint}) · ${n} season stacks`}
                    onClick={() => update({ det: d.id, bin: 0, pair: null })}
                    onKeyDown={(e) => rovingKeys(e, detIds, view.det, (id) => update({ det: id, bin: 0, pair: null }))}
                  >
                    <i style={{ background: d.color }} aria-hidden="true" />D{d.id}
                  </button>
                )
              })}
            </div>
          ) : (
            <div className="flex flex-wrap gap-1" role="group" aria-label="Detectors shown">
              {DETECTORS.map((d) => {
                const on = view.dets.includes(d.id)
                return (
                  <button
                    key={d.id}
                    type="button"
                    className={`tog det-chip ${on ? '' : 'is-off'}`}
                    aria-pressed={on}
                    onClick={() => update({ dets: toggled(view.dets, d.id) })}
                    title={`${d.band} (${d.hint}) · ${frameCounts[d.id] ?? 0} exposures`}
                  >
                    <i style={{ background: d.color, opacity: on ? 1 : 0.35 }} aria-hidden="true" />D{d.id}
                    <span className="tabular text-ink-500">{frameCounts[d.id] ?? 0}</span>
                  </button>
                )
              })}
            </div>
          )}
        </div>

      </div>

      <div className="toolbar-row">
        {diff && seq.pairs.length > 0 && (
          <div className="toolbar-group">
            <span className="field-label">Later − earlier</span>
            <div className="seg" role="group" aria-label="Seasons to subtract">
              {seq.pairs.map((p) => {
                const on = seq.pair?.[0] === p[0] && seq.pair?.[1] === p[1]
                return (
                  <button key={p.join('-')} type="button" aria-pressed={on} onClick={() => update({ pair: p })} title={`${seasonLabel(p[1])} minus ${seasonLabel(p[0])}`} className="tabular">
                    {shortSeason(seasonLabel(p[1]))} − {shortSeason(seasonLabel(p[0]))}
                  </button>
                )
              })}
            </div>
            <button
              type="button"
              className="tog"
              aria-pressed={view.diffCombined}
              onClick={() => update({ diffCombined: !view.diffCombined })}
              title="Combine all wavelength bins of the detector (each bin is differenced at matched wavelength first)"
            >
              <span className="dot" />
              All λ bins
            </button>
          </div>
        )}
        {diff && seq.pairs.length === 0 && (
          <span className="help">Not comparable: no two seasons of D{view.det} share a wavelength bin with 3 or more exposures each. Try another detector.</span>
        )}

        {showBins && (
          <label className="toolbar-group">
            <span className="field-label">λ bin</span>
            <select
              className="input min-h-7 max-w-[17rem] py-0 text-xs"
              value={seq.bin?.index ?? 0}
              onChange={(e) => update({ bin: Number(e.target.value) })}
              title="Official SPHEREx channels merged until each season has at least 3 exposures"
            >
              {seq.bins.map((b) => (
                <option key={b.id} value={b.index}>
                  {b.lam_lo_um.toFixed(2)}–{b.lam_hi_um.toFixed(2)} µm · R≈{b.resolving_power.toFixed(0)} ·{' '}
                  {Object.entries(b.counts)
                    .map(([s, n]) => `${seasonLabel(Number(s)).slice(2)}:${n}`)
                    .join(' ')}
                </option>
              ))}
            </select>
          </label>
        )}

        {!diff && field.seasons.length > 0 && (
          <div className="toolbar-group" role="group" aria-label="Seasons shown">
            <span className="field-label">Seasons</span>
            {field.seasons.map((s) => {
              const on = !view.hiddenSeasons.includes(s.season)
              return (
                <button
                  key={s.season}
                  type="button"
                  aria-pressed={on}
                  className={`tog tabular ${on ? '' : 'is-off'}`}
                  onClick={() => update({ hiddenSeasons: toggled(view.hiddenSeasons, s.season) })}
                  title={`${s.start} → ${s.end} · ${s.n_frames} exposures. Click to ${on ? 'hide' : 'show'} this season.`}
                >
                  {monthYear(s.start)}
                </button>
              )
            })}
          </div>
        )}

        <div className="toolbar-group toolbar-end" role="group" aria-label="Overlays" data-tour="overlays">
          <button type="button" className="tog" aria-pressed={view.marks} onClick={() => update({ marks: !view.marks })} title="Mark detections on the image">
            <span className="dot" />
            Detections
          </button>
          <button
            type="button"
            className="tog"
            aria-pressed={view.gaia}
            onClick={() => update({ gaia: !view.gaia })}
            title={overlay?.failed ?? 'Gaia DR3 and CatWISE2020 sources, moved to each image’s date'}
          >
            <span className="dot" />
            Catalogue{gaiaNote && <span className="text-ink-500">· {gaiaNote}</span>}
          </button>
          {!rgb && !diff && (
            <button
              type="button"
              className="tog"
              aria-pressed={view.flags}
              onClick={() => update({ flags: !view.flags })}
              title="Exposures: red = unusable pixel, yellow = suspect (outlier), magenta = instrument artefact. Stacks: red = no usable exposure."
            >
              <span className="dot" />
              Flags
            </button>
          )}
          <div>
            <Popover label="Adjust" button={<><IconSliders />Adjust</>} title="Display and difference settings">
              <div className="flex flex-col gap-4">
                <p className="help">{activeMode.help}</p>
                {diff && (
                  <div className="flex flex-col gap-2">
                    <p className="field-label">Difference colours</p>
                    <Range label={`Colour limit ±${view.zmax}σ`} title={SIGMA_HELP} min={3} max={20} step={1} value={view.zmax} onChange={(zmax) => update({ zmax })} />
                    <Range
                      label={`Colour from ${view.dfloor}σ`}
                      title="Changes dimmer than this keep the background colour, so only real change shows."
                      min={0}
                      max={4}
                      step={0.25}
                      value={view.dfloor}
                      onChange={(dfloor) => update({ dfloor })}
                    />
                    <Range
                      label={`Contrast γ ${view.dgamma.toFixed(1)}`}
                      title="Below 1 makes faint dipoles easier to see; above 1 makes strong ones stand out."
                      min={0.4}
                      max={2}
                      step={0.1}
                      value={view.dgamma}
                      onChange={(dgamma) => update({ dgamma })}
                    />
                    <Check
                      checked={view.diffLocal}
                      onChange={(diffLocal) => update({ diffLocal })}
                      title="Near a saturated star the residuals are much larger than the sky noise; scaling by the local noise stops those regions lighting up."
                    >
                      Scale by local noise (honest near bright stars)
                    </Check>
                  </div>
                )}
                {!diff && (
                  <div className="flex flex-col gap-2">
                    <p className="field-label">Display</p>
                    <div className="grid grid-cols-2 gap-2">
                      {!rgb && (
                        <label className="flex flex-col gap-1 text-xs text-ink-300" title="How brightness maps to grey. asinh shows faint and bright together; linear is the most literal.">
                          Stretch
                          <select value={view.render.stretch} onChange={(e) => setRender({ stretch: e.target.value as RenderOpts['stretch'] })} className="input text-xs">
                            <option value="asinh">asinh</option>
                            <option value="linear">linear</option>
                            <option value="sqrt">sqrt</option>
                            <option value="log">log</option>
                          </select>
                        </label>
                      )}
                      {!rgb && (
                        <label className="flex flex-col gap-1 text-xs text-ink-300" title={`Where black and white sit. Currently: ${eff}.`}>
                          Limits
                          <select value={view.render.scale} onChange={(e) => setRender({ scale: e.target.value as RenderOpts['scale'] })} className="input text-xs">
                            <option value="auto">auto</option>
                            <option value="noise">σ of each image</option>
                            <option value="detector">{kind === 'stack' ? 'σ shared by seasons' : 'percentiles per detector'}</option>
                            <option value="frame">percentiles per image</option>
                          </select>
                        </label>
                      )}
                    </div>
                    {sigma ? (
                      <div className="grid grid-cols-2 gap-3">
                        {!rgb && <Range label={`Black ${view.render.sigLo}σ`} title={SIGMA_HELP} min={-10} max={2} step={0.5} value={view.render.sigLo} onChange={(sigLo) => setRender({ sigLo })} />}
                        <Range label={`White ${view.render.sigHi}σ`} title={SIGMA_HELP} min={3} max={200} step={1} value={view.render.sigHi} onChange={(sigHi) => setRender({ sigHi })} />
                      </div>
                    ) : (
                      <div className="grid grid-cols-2 gap-3">
                        <Range label={`Black ${view.render.lo}%`} min={0} max={20} step={0.5} value={view.render.lo} onChange={(lo) => setRender({ lo })} />
                        <Range label={`White ${view.render.hi}%`} min={90} max={100} step={0.1} value={view.render.hi} onChange={(hi) => setRender({ hi })} />
                      </div>
                    )}
                    {!rgb && (
                      <Check checked={view.invert} onChange={(invert) => update({ invert })}>
                        Invert (dark stars on white)
                      </Check>
                    )}
                  </div>
                )}
                <div className="flex flex-col gap-1">
                  <p className="field-label">More overlays</p>
                  <Check checked={view.wise} onChange={(wise) => update({ wise })}>
                    AllWISE sources{wiseNote && <span className="text-ink-500"> · {wiseNote}</span>}
                  </Check>
                  {view.mode === 'frames' && (
                    <Check checked={view.hideFlagged} onChange={(hideFlagged) => update({ hideFlagged })}>
                      Hide exposures with a dead or unreliable pixel at the target
                    </Check>
                  )}
                </div>
              </div>
            </Popover>
          </div>
        </div>
      </div>
    </div>
  )
}
