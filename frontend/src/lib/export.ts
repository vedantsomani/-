// Client-side exports of a source fingerprint (no server round trip).
import type { FingerprintComplete, FingerprintPoint } from './api'

/** File-name stem from the target label, falling back to coordinates. */
export function exportStem(label: string, ra: number, dec: number): string {
  const slug = label
    .normalize('NFKD')
    .replace(/[^\w.+-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40)
  const coords = `ra${ra.toFixed(4)}_dec${dec.toFixed(4)}`
  return `spherex-fingerprint-${slug || coords}`
}

export function downloadText(filename: string, text: string, mime: string) {
  const url = URL.createObjectURL(new Blob([text], { type: `${mime};charset=utf-8` }))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

/** The full response plus where it came from and when it was exported. */
export function fingerprintJson(data: FingerprintComplete, pageUrl: string, now = new Date()): string {
  return JSON.stringify({ exported_at: now.toISOString(), page_url: pageUrl, fingerprint: data }, null, 2)
}

const CSV_COLUMNS = [
  'frame_id',
  'mjd',
  'date',
  'season',
  'detector',
  'wavelength_um',
  'bandwidth_um',
  'channel',
  'flux_ujy',
  'flux_err_ujy',
  'aperture_flux_ujy',
  'aperture_err_ujy',
  'quality',
  'flags',
] as const

const csvCell = (v: unknown): string => {
  if (v === null || v === undefined) return ''
  const s = String(v)
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

/** One row per exposure measurement; flags are joined with a space. */
export function pointsCsv(points: FingerprintPoint[]): string {
  const lines = [CSV_COLUMNS.join(',')]
  for (const p of points) {
    lines.push(
      CSV_COLUMNS.map((c) => csvCell(c === 'flags' ? (p.flags ?? []).join(' ') : p[c])).join(','),
    )
  }
  return lines.join('\r\n') + '\r\n'
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    // Clipboard API unavailable (insecure context, denied permission): fall back to a hidden textarea.
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    let ok = false
    try {
      ok = document.execCommand('copy')
    } catch {
      ok = false
    }
    ta.remove()
    return ok
  }
}
