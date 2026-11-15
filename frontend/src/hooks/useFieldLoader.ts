import { useCallback, useEffect, useState } from 'react'
import { apiFetch, fetchField, type FieldResponse } from '../lib/api'

const POLL_MS = 2000
/** Transient request failures (backend restarting, flaky network) are retried this many times. */
const MAX_QUIET_FAILURES = 3

interface Loaded {
  key: string
  field: FieldResponse | null
  error: string | null
  reconnecting: boolean
}

/** `refresh` ignores the cached manifest (the "nothing downloaded" retry); `update` re-queries the
 *  archive and processes only exposures not seen before (the "check for new observations" button). */
async function fetchFieldFlag(
  flag: 'refresh' | 'update', ra: number, dec: number, fov: number, signal?: AbortSignal,
): Promise<FieldResponse> {
  const q = new URLSearchParams({ ra: ra.toFixed(6), dec: dec.toFixed(6), fov: String(fov), [flag]: 'true' })
  const r = await apiFetch(`/api/frames?${q}`, { signal })
  if (!r.ok) {
    let detail = r.statusText
    try {
      detail = (await r.json()).detail ?? detail
    } catch {
      /* not JSON */
    }
    throw new Error(typeof detail === 'string' ? detail : `HTTP ${r.status}`)
  }
  return r.json() as Promise<FieldResponse>
}

/**
 * Requests a field and polls until the backend finishes building it, like `useField`, plus:
 * - `retry()` re-requests the same field (idempotent on the server);
 * - `retry({ rebuild: true })` asks the server to rebuild it (use when every exposure failed);
 * - `retry({ update: true })` checks the archive for new observations without disturbing what is shown;
 *   it resolves with the number of new exposures found and rejects if the archive could not be asked;
 * - brief request failures are retried quietly (`reconnecting`) before an error is shown.
 */
export function useFieldLoader(target: { ra: number; dec: number } | null, fov: number) {
  const ra = target?.ra ?? null
  const dec = target?.dec ?? null
  const [attempt, setAttempt] = useState({ n: 0, rebuild: false })
  const [loaded, setLoaded] = useState<Loaded | null>(null)
  const [pollNonce, setPollNonce] = useState(0) // restarts polling in place (same `key`, no flash)
  const key = `${ra}|${dec}|${fov}|${attempt.n}`

  useEffect(() => {
    if (ra === null || dec === null) return
    const ctrl = new AbortController()
    let timer: number | undefined
    let failures = 0
    let rebuild = attempt.rebuild

    const tick = async () => {
      try {
        const f = rebuild ? await fetchFieldFlag('refresh', ra, dec, fov, ctrl.signal) : await fetchField(ra, dec, fov, ctrl.signal)
        rebuild = false // only the first request of a rebuild retry carries refresh=true
        failures = 0
        const error = f.status === 'error' ? (f.error ?? 'The field could not be built.') : null
        setLoaded({ key, field: f, error, reconnecting: false })
        if (!error && f.status !== 'complete') timer = window.setTimeout(tick, POLL_MS)
      } catch (e) {
        if (ctrl.signal.aborted) return
        failures += 1
        if (failures <= MAX_QUIET_FAILURES) {
          setLoaded((l) => ({ key, field: l?.key === key ? l.field : null, error: null, reconnecting: true }))
          timer = window.setTimeout(tick, 1500 * failures)
        } else {
          setLoaded((l) => ({ key, field: l?.key === key ? l.field : null, error: (e as Error).message, reconnecting: false }))
        }
      }
    }
    tick()
    return () => {
      ctrl.abort()
      window.clearTimeout(timer)
    }
  }, [ra, dec, fov, attempt, key, pollNonce])

  const checkForNew = useCallback(async (): Promise<number> => {
    if (ra === null || dec === null) return 0
    const f = await fetchFieldFlag('update', ra, dec, fov)
    setLoaded((l) => (l && l.key === key ? { ...l, field: f, error: null } : l))
    if (f.status !== 'complete' && f.status !== 'error') setPollNonce((n) => n + 1) // new exposures are processing
    return f.new_frames ?? 0
  }, [ra, dec, fov, key])

  const retry = useCallback(
    (opts?: { rebuild?: boolean; update?: boolean }): Promise<number> | void => {
      if (opts?.update) return checkForNew()
      setAttempt((a) => ({ n: a.n + 1, rebuild: !!opts?.rebuild }))
    },
    [checkForNew],
  )

  const current = ra !== null && loaded?.key === key ? loaded : null
  return {
    field: current?.field ?? null,
    error: current?.error ?? null,
    reconnecting: current?.reconnecting ?? false,
    retry,
  }
}
