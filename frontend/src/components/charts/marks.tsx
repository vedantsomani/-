// Marker shapes shared by the fingerprint charts. Colours and season styles live in chartStyle.ts.

import type { MarkerShape } from './chartStyle'

export interface MarkerProps {
  shape: MarkerShape
  x: number
  y: number
  /** Half-width of the marker in px. */
  r: number
  color: string
  /** Hollow marker (outline only). */
  hollow?: boolean
  opacity?: number
}

export function Marker({ shape, x, y, r, color, hollow, opacity = 1 }: MarkerProps) {
  const common = {
    fill: hollow ? 'none' : color,
    stroke: color,
    strokeWidth: hollow ? 1.5 : 0,
    opacity,
  }
  const k = r * 1.25
  switch (shape) {
    case 'circle':
      return <circle cx={x} cy={y} r={r} {...common} />
    case 'square':
      return <rect x={x - r * 0.9} y={y - r * 0.9} width={r * 1.8} height={r * 1.8} {...common} />
    case 'diamond':
      return <polygon points={`${x},${y - k} ${x + k},${y} ${x},${y + k} ${x - k},${y}`} {...common} />
    case 'triangle':
      return <polygon points={`${x},${y - k} ${x + k},${y + k * 0.8} ${x - k},${y + k * 0.8}`} {...common} />
    case 'triangleDown':
      return <polygon points={`${x},${y + k} ${x + k},${y - k * 0.8} ${x - k},${y - k * 0.8}`} {...common} />
    case 'cross':
      return (
        <path
          d={`M${x - k},${y} H${x + k} M${x},${y - k} V${y + k}`}
          fill="none"
          stroke={color}
          strokeWidth={2}
          strokeLinecap="round"
          opacity={opacity}
        />
      )
  }
}

/** Marker + 1-sigma error bar, y positions already in pixels. */
export function ErrorBar({ x, y0, y1, color, opacity = 0.7 }: { x: number; y0: number; y1: number; color: string; opacity?: number }) {
  return <line x1={x} x2={x} y1={y0} y2={y1} stroke={color} strokeWidth={1} opacity={opacity} />
}

/** Inline key for legends and tooltips (a marker drawn in a tiny SVG). */
export function MarkerKey({ shape, color, hollow, size = 14 }: { shape: MarkerShape; color: string; hollow?: boolean; size?: number }) {
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true" className="shrink-0">
      <Marker shape={shape} x={size / 2} y={size / 2} r={size * 0.28} color={color} hollow={hollow} />
    </svg>
  )
}
