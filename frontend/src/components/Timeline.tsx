import { useMemo } from 'react'
import type { Season } from '../lib/api'
import { detectorColor } from '../lib/constants'
import type { BlinkItem } from '../lib/items'
import { useElementWidth } from '../hooks/useElementWidth'

interface Props {
  items: BlinkItem[] // the images currently in the blink sequence, in order
  seasons: Season[]
  current: number
  onSelect: (i: number) => void
}

const PAD = 14
/** Below this width the season segments wrap into rows of two (phones) instead of shrinking. */
const NARROW_PX = 560
/** A tap/click selects the nearest image within this many CSS pixels (fingers are fat). */
const PICK_MOUSE_PX = 10
const PICK_TOUCH_PX = 26
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function shortDate(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso)
  return m ? `${MONTHS[Number(m[2]) - 1]} ${Number(m[3])}` : iso
}

/** Full ISO dates when the segment is wide enough, otherwise "Apr 21 – May 3 '26". */
function rangeLabel(s: Season, w: number): string {
  if (w >= 175) return s.start === s.end ? s.start : `${s.start} → ${s.end}`
  const a = shortDate(s.start)
  const b = shortDate(s.end)
  return `${a === b ? a : `${a} – ${b}`} '${s.start.slice(2, 4)}`
}

/** Broken time axis: each survey season gets its own segment (seasons are months apart,
 *  observations within one are days apart). One row per detector. Stacks are drawn as
 *  bars spanning the exposures they combine. Drawn in real pixels so text and dots stay
 *  readable on a phone, where the segments wrap into two columns. Tap or drag anywhere
 *  to pick the nearest image. */
export function Timeline({ items, seasons, current, onSelect }: Props) {
  const [ref, width] = useElementWidth<HTMLDivElement>(720)

  const layout = useMemo(() => {
    const used = seasons.filter((s) => items.some((f) => f.season === s.season))
    // One row per detector actually in the sequence (one row for single-detector modes).
    const dets = [...new Set(items.map((f) => f.detector))].sort((a, b) => a - b)
    const nRows = Math.max(1, dets.length)
    const row = (det: number) => Math.max(0, dets.indexOf(det))
    const narrow = width < NARROW_PX
    const cols = Math.max(1, narrow ? Math.min(2, used.length) : used.length)
    const rowH = narrow ? 12 : 10
    const gap = narrow ? 14 : 18
    const segW = (width - 2 * PAD - gap * (cols - 1)) / cols
    const blockH = nRows * rowH + 26 // detector rows + date label + spacing
    const blocks = used.map((s, i) => ({
      season: s,
      x0: PAD + (i % cols) * (segW + gap),
      y0: 2 + Math.floor(i / cols) * blockH,
    }))
    const block = (season: number) => blocks.find((b) => b.season.season === season)
    const xAt = (season: number, mjd: number) => {
      const b = block(season)
      if (!b) return -100
      const span = Math.max(b.season.mjd_end - b.season.mjd_start, 1e-3)
      return b.x0 + Math.min(Math.max((mjd - b.season.mjd_start) / span, 0), 1) * segW
    }
    const yAt = (season: number, det: number) => (block(season)?.y0 ?? -100) + 3 + (row(det) + 0.5) * rowH
    const height = 2 + Math.max(1, Math.ceil(used.length / cols)) * blockH
    return { blocks, segW, rowH, nRows, height, xAt, yAt, block }
  }, [items, seasons, width])

  const cur = items[current]
  const curBlock = cur ? layout.block(cur.season) : undefined

  const pick = (e: React.PointerEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect()
    const px = e.clientX - r.left
    const py = e.clientY - r.top
    const reach = e.pointerType === 'mouse' ? PICK_MOUSE_PX : PICK_TOUCH_PX
    let best = -1
    let bestD = reach * reach
    items.forEach((f, i) => {
      const dx = layout.xAt(f.season, f.mjd) - px
      const dy = layout.yAt(f.season, f.detector) - py
      const d = dx * dx + dy * dy
      if (d < bestD) {
        bestD = d
        best = i
      }
    })
    if (best >= 0 && best !== current) onSelect(best)
  }

  return (
    <div ref={ref} className="w-full">
      <svg
        width={width}
        height={layout.height}
        viewBox={`0 0 ${width} ${layout.height}`}
        className="block max-w-full cursor-pointer select-none"
        style={{ touchAction: 'pan-y' }}
        role="img"
        aria-label="Observation timeline: one segment per season, one row per detector; click to jump to an image"
        onPointerDown={pick}
        onPointerMove={(e) => e.buttons > 0 && pick(e)}
      >
        {layout.blocks.map((b) => (
          <g key={b.season.season}>
            <rect x={b.x0 - 4} y={b.y0} width={layout.segW + 8} height={layout.nRows * layout.rowH + 6} rx={3} className="fill-ink-900" />
            <text x={b.x0} y={b.y0 + layout.nRows * layout.rowH + 19} className="fill-ink-500 tabular" fontSize={11}>
              {rangeLabel(b.season, layout.segW)}
            </text>
          </g>
        ))}
        {items.map((f, i) => {
          const on = i === current
          const color = f.detector > 0 ? detectorColor(f.detector) : '#ececef'
          const cy = layout.yAt(f.season, f.detector)
          return (
            <g key={f.id}>
              {f.mjdMin !== undefined && f.mjdMax !== undefined && (
                <rect
                  x={layout.xAt(f.season, f.mjdMin)}
                  y={cy - 2}
                  width={Math.max(layout.xAt(f.season, f.mjdMax) - layout.xAt(f.season, f.mjdMin), 2)}
                  height={4}
                  rx={2}
                  fill={color}
                  opacity={on ? 0.9 : 0.35}
                />
              )}
              <circle
                cx={layout.xAt(f.season, f.mjd)}
                cy={cy}
                r={on ? 4.5 : 2.6}
                fill={color}
                opacity={on ? 1 : 0.6}
                stroke={on ? '#fff' : 'none'}
                strokeWidth={1.5}
              >
                <title>{`${f.title} · ${f.subtitle}`}</title>
              </circle>
            </g>
          )
        })}
        {cur && curBlock && (
          <line
            x1={layout.xAt(cur.season, cur.mjd)}
            x2={layout.xAt(cur.season, cur.mjd)}
            y1={curBlock.y0}
            y2={curBlock.y0 + layout.nRows * layout.rowH + 6}
            stroke="#5c9cff"
            strokeWidth={1}
            opacity={0.6}
            pointerEvents="none"
          />
        )}
      </svg>
    </div>
  )
}
