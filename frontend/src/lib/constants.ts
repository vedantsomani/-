// SPHEREx detectors: D1 bluest ... D6 reddest. Approximate band edges (um); lo/hi are numeric (um).
export const DETECTORS = [
  { id: 1, lo: 0.75, hi: 1.09, band: '0.75–1.09 µm', color: '#5b8cff', hint: 'bluest, near-infrared' },
  { id: 2, lo: 1.10, hi: 1.62, band: '1.10–1.62 µm', color: '#3fc6e8', hint: 'near-infrared' },
  { id: 3, lo: 1.63, hi: 2.42, band: '1.63–2.42 µm', color: '#4fe0a4', hint: 'where ordinary stars are brightest' },
  { id: 4, lo: 2.42, hi: 3.82, band: '2.42–3.82 µm', color: '#d9e94a', hint: 'mid-infrared' },
  { id: 5, lo: 3.83, hi: 4.41, band: '3.83–4.41 µm', color: '#ffae4a', hint: 'cold objects start to glow' },
  { id: 6, lo: 4.42, hi: 5.00, band: '4.42–5.00 µm', color: '#ff6b81', hint: 'reddest, best for cold brown dwarfs' },
] as const

export const detectorColor = (d: number) => DETECTORS[d - 1]?.color ?? '#999'

/** Text labels use theme-aware ink; canvas marks retain their detector colours. */
export const detectorTextColor = (d: number) => `var(--b${d})`

export const FOV_OPTIONS = [2, 3, 5, 10, 20, 30] // arcmin

export interface ExampleTarget {
  id: string
  name: string
  /** Short category chip on the card. */
  tag: string
  /** One plain-language line: what to look for. */
  blurb: string
  /** Exact cached view URL (coordinates + proper motion, so it needs no name lookup). */
  search: string
  /** Real motion per year or variability description */
  motionPerYear: string
  /** Optimal SPHEREx detector band */
  bestDetector: number
}

// Search strings are the exact URLs of fields that are cached on the server: coordinates and proper
// motion are spelled out, so opening an example is instant and never depends on the name resolver.
export const EXAMPLE_TARGETS: ExampleTarget[] = [
  {
    id: 'wise0855',
    name: 'WISE 0855−0714',
    tag: 'Coldest brown dwarf',
    blurb:
      'The coldest known brown dwarf, creeping across the sky at about 8″ a year. It is only visible at 4–5 µm, so look at detector D6.',
    search: '?ra=133.760020&dec=-7.242207&name=WISE%200855-0714&pmra=-8123.7&pmdec=673.2&pmepoch=61000&det=6',
    motionPerYear: '8.15″ / yr',
    bestDetector: 6,
  },
  {
    id: 'barnard',
    name: "Barnard's Star",
    tag: 'Fastest-moving star',
    blurb: 'The fastest-moving star in the sky, about 10″ a year. Watch it slide past the background stars.',
    search: '?ra=269.446290&dec=4.767880&name=Barnard%27s%20Star&pmra=-801.6&pmdec=10362.4&pmepoch=61000&det=3',
    motionPerYear: '10.39″ / yr',
    bestDetector: 3,
  },
  {
    id: 'epsindib',
    name: 'Epsilon Indi B',
    tag: 'Brown dwarf pair',
    blurb: 'A pair of methane brown dwarfs moving 4.7″ a year. Too close together to split, so they move as one dot.',
    search: '?ra=331.096420&dec=-56.800580&name=eps%20Ind%20B&pmra=3981.9&pmdec=-2466.8&pmepoch=61000&det=4',
    motionPerYear: '4.68″ / yr',
    bestDetector: 4,
  },
  {
    id: 'rwaur',
    name: 'RW Aurigae',
    tag: 'Variable young star',
    blurb: 'A young star that fades and brightens. Compare its brightness from season to season.',
    search: '?ra=76.956530&dec=30.401440&name=RW%20Aur&det=2',
    motionPerYear: 'Var (<0.02″)',
    bestDetector: 2,
  },
  {
    id: 'rdra',
    name: 'R Draconis',
    tag: 'Pulsating red giant',
    blurb: 'A pulsating red giant that swells and shrinks over months. Its brightness changes between seasons.',
    search: '?ra=248.167610&dec=66.754980&name=R%20Dra&det=3',
    motionPerYear: 'Var (<0.01″)',
    bestDetector: 3,
  },
]

/** The example the guided tour loads. */
export const TOUR_EXAMPLE = EXAMPLE_TARGETS[0]

/** DOM ids that tie the view-mode tabs to the blink player (their tab panel). */
export const BLINK_PANEL_ID = 'blink-panel'
export const viewTabId = (mode: string) => `view-tab-${mode}`
