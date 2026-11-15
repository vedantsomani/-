import { useCallback, useEffect, useState } from 'react'
import { HttpError, cancelBatch, fetchBatch, type BatchData } from '../lib/batch'

const POLL_MS = 4000

interface State {
  id: string | null
  data: BatchData | null
  error: string | null
}

/** Load a batch and poll every 4 s while it is running. Pass null to stay idle. */
export function useBatch(id: string | null) {
  const [state, setState] = useState<State>({ id: null, data: null, error: null })

  useEffect(() => {
    if (id === null) return
    const ctrl = new AbortController()
    let timer: number | undefined
    const tick = async () => {
      try {
        const data = await fetchBatch(id, ctrl.signal)
        if (ctrl.signal.aborted) return
        setState({ id, data, error: null })
        if (data.status === 'running') timer = window.setTimeout(tick, POLL_MS)
      } catch (e) {
        if (ctrl.signal.aborted) return
        const notFound = e instanceof HttpError && e.status === 404
        setState((s) => ({ id, data: s.id === id ? s.data : null, error: (e as Error).message }))
        if (!notFound) timer = window.setTimeout(tick, POLL_MS) // network blip: keep trying
      }
    }
    void tick()
    return () => {
      ctrl.abort()
      window.clearTimeout(timer)
    }
  }, [id])

  const cancel = useCallback(async () => {
    if (id === null) return
    try {
      const data = await cancelBatch(id)
      setState({ id, data, error: null })
    } catch (e) {
      setState((s) => ({ ...s, error: (e as Error).message }))
    }
  }, [id])

  const current = state.id === id ? state : null
  return { data: current?.data ?? null, error: current?.error ?? null, cancel }
}
