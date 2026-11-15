import { useEffect, useState } from 'react'
import { fetchHoldout, type HoldoutResponse } from '../lib/holdout'

const POLL_MS = 3000

interface State {
  /** Query the data belongs to; a stale key means "nothing yet" for the current request. */
  key: string | null
  data: HoldoutResponse | null
  error: string | null
}

/**
 * Request the prediction test and poll while the backend is still working on it. Pass null to
 * stay idle (nothing is fetched until the user runs the test). Changing the query aborts the
 * request in flight and starts over.
 */
export function useHoldout(query: string | null) {
  const [state, setState] = useState<State>({ key: null, data: null, error: null })
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    if (query === null) return
    const ctrl = new AbortController()
    let timer: number | undefined

    const tick = async () => {
      try {
        const r = await fetchHoldout(query, ctrl.signal)
        if (ctrl.signal.aborted) return
        setState({ key: query, data: r, error: r.status === 'error' ? r.error || 'the prediction test failed' : null })
        if (r.status === 'processing') timer = window.setTimeout(tick, POLL_MS)
      } catch (e) {
        if (ctrl.signal.aborted) return
        const error = (e as Error).message
        setState((s) => ({ key: query, data: s.key === query ? s.data : null, error }))
      }
    }
    void tick()
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
