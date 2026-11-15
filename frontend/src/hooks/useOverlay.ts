import { useEffect, useState } from 'react'
import { fetchOverlay } from '../lib/api'
import type { OverlayData } from '../lib/overlay'

const POLL_MS = 3000
const MAX_ERRORS = 3

interface Result {
  key: string
  data: OverlayData | null
  failed: string | null
}

export interface OverlayState {
  data: OverlayData | null
  loading: boolean
  /** why Gaia is unavailable (request failed, or the catalogue query did); null when fine */
  failed: string | null
}

/** Gaia overlay for a field. Fetches only when enabled; polls while the backend is still working. */
export function useOverlay(fieldKey: string | null, enabled: boolean): OverlayState {
  const [res, setRes] = useState<Result | null>(null)

  useEffect(() => {
    if (!fieldKey || !enabled) return
    const ctrl = new AbortController()
    let timer: number | undefined
    let errors = 0
    const done = (data: OverlayData | null, failed: string | null) => setRes({ key: fieldKey, data, failed })

    const tick = async () => {
      try {
        const r = await fetchOverlay(fieldKey, ctrl.signal)
        errors = 0
        if (r.status === 'processing') timer = window.setTimeout(tick, POLL_MS)
        else if (r.status === 'error') done(null, r.error)
        else done(r, r.catalog.ok ? null : (r.catalog.note ?? 'catalogue query failed'))
      } catch (e) {
        if (ctrl.signal.aborted) return
        if (++errors >= MAX_ERRORS) done(null, (e as Error).message)
        else timer = window.setTimeout(tick, POLL_MS)
      }
    }
    tick()
    return () => {
      ctrl.abort()
      window.clearTimeout(timer)
    }
  }, [fieldKey, enabled])

  const mine = enabled && fieldKey && res?.key === fieldKey ? res : null
  return { data: mine?.data ?? null, loading: enabled && !!fieldKey && !mine, failed: mine?.failed ?? null }
}
