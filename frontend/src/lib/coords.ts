import { pixToSky } from './wcs'
// Coordinate parsing/formatting. Mirrors backend/app/coords.py so URLs and the
// search box accept the same formats: decimal degrees or sexagesimal.

const DECIMAL = /^[+-]?\d+(\.\d*)?([eE][+-]?\d+)?$/

/** Split a sexagesimal string ("08h55m10.8s", "08:55:10.8", "08 55 10.8") into numbers. */
function sexParts(s: string): { sign: number; parts: number[] } | null {
  const t = s.trim()
  const sign = t.startsWith('-') ? -1 : 1
  const body = t.replace(/^[+-]/, '')
  const parts = body
    .split(/[hdms:°'"′″\s]+/i)
    .filter(Boolean)
    .map(Number)
  if (parts.length === 0 || parts.length > 3 || parts.some((p) => !Number.isFinite(p))) return null
  return { sign, parts }
}

function sexToValue(s: string): number | null {
  const sp = sexParts(s)
  if (!sp) return null
  const [a, b = 0, c = 0] = sp.parts
  return sp.sign * (a + b / 60 + c / 3600)
}

export function parseRa(s: string): number | null {
  const t = s.trim()
  const v = DECIMAL.test(t) ? Number(t) : (() => {
    const h = sexToValue(t)
    return h === null ? null : h * 15
  })()
  return v !== null && v >= 0 && v < 360 ? v : null
}

export function parseDec(s: string): number | null {
  const t = s.trim()
  const v = DECIMAL.test(t) ? Number(t) : sexToValue(t)
  return v !== null && v >= -90 && v <= 90 ? v : null
}

/** Parse a free-form "RA Dec" string. Returns null if it doesn't look like coordinates. */
export function parsePosition(text: string): { ra: number; dec: number } | null {
  const s = text.trim().replace(/,/g, ' ')
  if (!/\d/.test(s)) return null
  const tokens = s.split(/\s+/)
  if (tokens.length === 2) {
    const ra = parseRa(tokens[0])
    const dec = parseDec(tokens[1])
    if (ra !== null && dec !== null) return { ra, dec }
  }
  if (tokens.length === 6) {
    const ra = parseRa(tokens.slice(0, 3).join(':'))
    const dec = parseDec(tokens.slice(3).join(':'))
    if (ra !== null && dec !== null) return { ra, dec }
  }
  return null
}

function pad(n: number, width: number, decimals = 0): string {
  const s = n.toFixed(decimals)
  const [i, f] = s.split('.')
  return i.padStart(width, '0') + (f ? '.' + f : '')
}

function toSex(value: number, decimals: number): [number, number, number] {
  let a = Math.floor(value)
  let b = Math.floor((value - a) * 60)
  let c = Number(((value - a - b / 60) * 3600).toFixed(decimals))
  if (c >= 60) { c -= 60; b += 1 }
  if (b >= 60) { b -= 60; a += 1 }
  return [a, b, c]
}

export function formatRa(deg: number): string {
  const [h, m, s] = toSex(deg / 15, 2)
  return `${pad(h, 2)}h${pad(m, 2)}m${pad(s, 2, 2)}s`
}

export function formatDec(deg: number): string {
  const sign = deg < 0 ? '-' : '+'
  const [d, m, s] = toSex(Math.abs(deg), 1)
  return `${sign}${pad(d, 2)}°${pad(m, 2)}′${pad(s, 2, 1)}″`
}

/** Modified Julian Date -> JS Date. */
export function mjdToDate(mjd: number): Date {
  return new Date((mjd - 40587) * 86400000)
}

export const J2000_MJD = 51544.5
/** Reference epoch for field centres of moving targets: mid SPHEREx survey (2025-11). */
export const SPHEREX_REF_MJD = 61000

/** Proper motion of a target, relative to its position at `epochMjd`. */
export interface ProperMotion {
  pmra: number // mas/yr, includes cos(dec)
  pmdec: number // mas/yr
  epochMjd: number
}

/** Linear proper-motion propagation (ignores parallax: < 1" for anything we target). */
export function propagate(ra: number, dec: number, pm: ProperMotion, toMjd: number): { ra: number; dec: number } {
  const dtYr = (toMjd - pm.epochMjd) / 365.25
  const dDec = (pm.pmdec * dtYr) / 3.6e6
  const dRa = (pm.pmra * dtYr) / 3.6e6 / Math.cos((dec * Math.PI) / 180)
  return { ra: (((ra + dRa) % 360) + 360) % 360, dec: dec + dDec }
}

/** Offset (arcsec, +east/+north) of the target at `mjd` from its position at pm.epochMjd. */
export function pmOffsetArcsec(pm: ProperMotion, mjd: number): { east: number; north: number } {
  const dtYr = (mjd - pm.epochMjd) / 365.25
  return { east: (pm.pmra * dtYr) / 1000, north: (pm.pmdec * dtYr) / 1000 }
}

export function gridPixToRaDec(
  x: number,
  y: number,
  gridN: number,
  pixscaleArcsec: number,
  ra0Deg: number,
  dec0Deg: number,
): { ra: number; dec: number } {
  return pixToSky({ ra0: ra0Deg, dec0: dec0Deg, crpix: [(gridN+1)/2, (gridN+1)/2],
    m: [[-pixscaleArcsec/3600, 0], [0, pixscaleArcsec/3600]] }, x, y)
}
