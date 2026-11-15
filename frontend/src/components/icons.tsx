import type { SVGProps } from 'react'

type P = SVGProps<SVGSVGElement>

/** One thin-stroke icon set (24 px grid, 1.6 stroke, round caps). Transport glyphs are filled. */
const base = (p: P): P => ({
  viewBox: '0 0 24 24',
  width: 16,
  height: 16,
  'aria-hidden': true,
  focusable: false,
  ...p,
})
const stroke = (p: P): P => ({
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.6,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  ...base(p),
})

export const IconPlay = (p: P) => (
  <svg {...base(p)} fill="currentColor">
    <path d="M8 5.6v12.8a.6.6 0 0 0 .9.5l10.2-6.4a.6.6 0 0 0 0-1L8.9 5.1a.6.6 0 0 0-.9.5z" />
  </svg>
)
export const IconPause = (p: P) => (
  <svg {...base(p)} fill="currentColor">
    <rect x="6.5" y="5" width="3.8" height="14" rx=".8" />
    <rect x="13.7" y="5" width="3.8" height="14" rx=".8" />
  </svg>
)
export const IconPrev = (p: P) => (
  <svg {...base(p)} fill="currentColor">
    <rect x="5.5" y="5.5" width="2.2" height="13" rx=".6" />
    <path d="M18.5 6.2v11.6a.6.6 0 0 1-.9.5L9.4 12.5a.6.6 0 0 1 0-1l8.2-5.8a.6.6 0 0 1 .9.5z" />
  </svg>
)
export const IconNext = (p: P) => (
  <svg {...base(p)} fill="currentColor">
    <rect x="16.3" y="5.5" width="2.2" height="13" rx=".6" />
    <path d="M5.5 6.2v11.6a.6.6 0 0 0 .9.5l8.2-5.8a.6.6 0 0 0 0-1L6.4 5.7a.6.6 0 0 0-.9.5z" />
  </svg>
)
export const IconChevronDown = (p: P) => (
  <svg {...stroke({ width: 14, height: 14, ...p })}>
    <path d="m6 9 6 6 6-6" />
  </svg>
)
export const IconHelp = (p: P) => (
  <svg {...stroke(p)}>
    <circle cx="12" cy="12" r="9" />
    <path d="M9.6 9.4a2.5 2.5 0 0 1 4.8.9c0 1.7-2.4 2.2-2.4 3.7M12 17h.01" />
  </svg>
)
export const IconLink = (p: P) => (
  <svg {...stroke(p)}>
    <path d="M10 13a5 5 0 0 0 7.1 0l2.8-2.8a5 5 0 0 0-7.1-7.1L11.5 4.4M14 11a5 5 0 0 0-7.1 0l-2.8 2.8a5 5 0 0 0 7.1 7.1l1.3-1.3" />
  </svg>
)
export const IconArrowRight = (p: P) => (
  <svg {...stroke(p)}>
    <path d="M5 12h14M13 6l6 6-6 6" />
  </svg>
)
export const IconClose = (p: P) => (
  <svg {...stroke(p)}>
    <path d="M6 6l12 12M18 6 6 18" />
  </svg>
)
export const IconKeyboard = (p: P) => (
  <svg {...stroke(p)}>
    <rect x="2.5" y="5" width="19" height="14" rx="2" />
    <path d="M6.5 9h.01M10 9h.01M14 9h.01M17.5 9h.01M6.5 12.5h.01M10 12.5h.01M14 12.5h.01M17.5 12.5h.01M8 16h8" />
  </svg>
)
export const IconSparkles = (p: P) => (
  <svg {...stroke(p)}>
    <path d="M12 4v4M12 16v4M4 12h4M16 12h4" />
  </svg>
)
export const IconDownload = (p: P) => (
  <svg {...stroke(p)}>
    <path d="M12 4v11M7 10l5 5 5-5M5 19h14" />
  </svg>
)
export const IconSearch = (p: P) => (
  <svg {...stroke(p)}>
    <circle cx="11" cy="11" r="6.5" />
    <path d="m16 16 4 4" />
  </svg>
)
export const IconCommand = (p: P) => (
  <svg {...stroke(p)}>
    <path d="M9 6.5A2.5 2.5 0 1 0 6.5 9H9V6.5zM9 9h6v6H9zM15 6.5A2.5 2.5 0 1 1 17.5 9H15V6.5zM9 17.5A2.5 2.5 0 1 1 6.5 15H9v2.5zM15 17.5a2.5 2.5 0 1 0 2.5-2.5H15v2.5z" />
  </svg>
)
export const IconReport = (p: P) => (
  <svg {...stroke(p)}>
    <path d="M7 3.5h7l4 4V20a.5.5 0 0 1-.5.5h-10.5a.5.5 0 0 1-.5-.5V4a.5.5 0 0 1 .5-.5z" />
    <path d="M13.5 3.5V8H18M9 12h6M9 15.5h6" />
  </svg>
)
export const IconImage = (p: P) => (
  <svg {...stroke(p)}>
    <rect x="3.5" y="4.5" width="17" height="15" rx="2" />
    <path d="m4 17 5-5 4 4 2.5-2.5L20 18" />
    <circle cx="15.5" cy="9" r="1.3" />
  </svg>
)
export const IconRefresh = (p: P) => (
  <svg {...stroke(p)}>
    <path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v4h-4" />
  </svg>
)
export const IconExpand = (p: P) => (
  <svg {...stroke(p)}>
    <path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7" />
  </svg>
)
export const IconCollapse = (p: P) => (
  <svg {...stroke(p)}>
    <path d="M20 10h-6V4M4 14h6v6M14 10l7-7M10 14l-7 7" />
  </svg>
)
export const IconFullscreen = (p: P) => (
  <svg {...stroke(p)}>
    <path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" />
  </svg>
)
export const IconRuler = (p: P) => (
  <svg {...stroke(p)}>
    <path d="m3.5 15.5 12-12 5 5-12 12zM7.5 11.5l2 2M10.5 8.5l2 2M13.5 5.5l2 2" />
  </svg>
)
export const IconSliders = (p: P) => (
  <svg {...stroke(p)}>
    <path d="M4 7h10M18 7h2M4 17h4M12 17h8" />
    <circle cx="16" cy="7" r="2" />
    <circle cx="10" cy="17" r="2" />
  </svg>
)
export const IconLayers = (p: P) => (
  <svg {...stroke(p)}>
    <path d="m12 4 8.5 4.5L12 13 3.5 8.5z" />
    <path d="m3.5 12.5 8.5 4.5 8.5-4.5" />
  </svg>
)
export const IconList = (p: P) => (
  <svg {...stroke(p)}>
    <path d="M9 6.5h11M9 12h11M9 17.5h11M4.5 6.5h.01M4.5 12h.01M4.5 17.5h.01" />
  </svg>
)
export const IconCheck = (p: P) => (
  <svg {...stroke(p)}>
    <path d="m5 12.5 4.5 4.5L19 7.5" />
  </svg>
)
export const IconCross = (p: P) => (
  <svg {...stroke(p)}>
    <path d="M7 7l10 10M17 7 7 17" />
  </svg>
)
export const IconTarget = (p: P) => (
  <svg {...stroke(p)}>
    <circle cx="12" cy="12" r="7" />
    <path d="M12 2.5V6M12 18v3.5M2.5 12H6M18 12h3.5" />
  </svg>
)
export const IconHome = (p: P) => (
  <svg {...stroke(p)}>
    <path d="M4 11 12 4.5 20 11M6.5 9.5V19.5h11V9.5" />
  </svg>
)

export const IconPalette = (p: P) => (
  <svg {...stroke(p)}>
    <path d="M12 3.5a8.5 8.5 0 1 0 0 17c1.1 0 1.6-.7 1.6-1.5 0-1.2-1-1.6-1-2.6 0-.9.7-1.4 1.6-1.4h2.1a4.2 4.2 0 0 0 4.2-4.2C20.5 6.9 16.7 3.5 12 3.5z" />
    <circle cx="7.8" cy="11" r="1" />
    <circle cx="10.5" cy="7.6" r="1" />
    <circle cx="15" cy="7.8" r="1" />
  </svg>
)

/** GAMAS mark: a framed field of sky with one source caught at two epochs (hollow earlier, solid later). */
export const BrandMark = (p: P) => (
  <svg {...base({ width: 24, height: 24, ...p })} viewBox="0 0 32 32">
    <rect x="1.5" y="1.5" width="29" height="29" rx="7" fill="#111317" stroke="#3b4048" />
    <circle cx="8.5" cy="23.5" r="1.1" fill="#858a94" />
    <circle cx="23.5" cy="8" r="1.1" fill="#858a94" />
    <circle cx="10.5" cy="11.5" r="3" fill="none" stroke="#5c9cff" strokeWidth="1.5" strokeDasharray="2.1 1.7" />
    <path d="M13 13.6 18 17.6" stroke="#5c9cff" strokeWidth="1.5" strokeLinecap="round" opacity=".6" />
    <circle cx="20.8" cy="19.8" r="3.1" fill="#ececef" />
  </svg>
)
/** Kept for older imports. */
export const Logo = BrandMark
