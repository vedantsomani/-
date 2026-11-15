import { useEffect, useState } from 'react'
import { fetchFingerprint, fingerprintQuery, type FingerprintRequest, type FingerprintResponse } from '../lib/api'

const POLL_MS = 3000

interface State {
  /** Query the data belongs to; a stale key means "nothing yet" for the current request. */
  key: string | null
  data: FingerprintResponse | null
  error: string | null
}

/**
 * Request the source fingerprint and poll while the backend is still working on it.
 * Pass null to stay idle (nothing is fetched until the section is opened). Changing the
 * target, field size or proper motion aborts the in-flight request and starts over.
 */
export function useFingerprint(req: FingerprintRequest | null) {
  const [state, setState] = useState<State>({ key: null, data: null, error: null })
  const [attempt, setAttempt] = useState(0)
  const query = req ? fingerprintQuery(req) : null

  useEffect(() => {
    if (query === null) return
    const ctrl = new AbortController()
    let timer: number | undefined

    const tick = async () => {
      try {
        const r = await fetchFingerprint(query, ctrl.signal)
        if (ctrl.signal.aborted) return
        setState({ key: query, data: r, error: r.status === 'error' ? r.error || 'fingerprint failed' : null })
        if (r.status === 'processing') timer = window.setTimeout(tick, POLL_MS)
      } catch (e) {
        if (ctrl.signal.aborted) return
        const error = (e as Error).message
        setState((s) => ({ key: query, data: s.key === query ? s.data : null, error }))
      }
    }
    tick()
    return () => {
      ctrl.abort()
      window.clearTimeout(timer)
    }
  }, [query, attempt])

  const current = state.key === query ? state : null
  return {
    data: current?.data ?? null,
    error: current?.error ?? null,
    retry: () => {
      setState({ key: query, data: null, error: null })
      setAttempt((a) => a + 1)
    },
  }
}
