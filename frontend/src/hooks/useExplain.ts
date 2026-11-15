import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchExplain, isReady, type ExplainAi, type ExplainReady, type ExplainResponse } from '../lib/explain'

type Busy = 'facts' | 'ai' | null

interface State {
  /** Field + candidate the data belongs to; a stale id means "nothing yet" for the current candidate. */
  id: string
  response: ExplainResponse | null
  busy: Busy
  /** Why the facts could not be loaded. */
  error: string | null
  /** Why the optional AI rewrite is missing (network failure on our side; the server's reason is in the response). */
  aiError: string | null
}

const empty = (id: string): State => ({ id, response: null, busy: null, error: null, aiError: null })

export interface UseExplain {
  /** The facts-layer answer once it has loaded. */
  ready: ExplainReady | null
  /** True while the field is still being analysed on the server. */
  processing: boolean
  busy: Busy
  error: string | null
  ai: ExplainAi | null
  aiError: string | null
  explain: () => void
  rewrite: () => void
}

/**
 * Fetch the plain-language explanation of one candidate. Nothing is requested until `explain()`
 * is called; `rewrite()` asks the server for the optional AI rewrite, and a failure there leaves
 * the facts untouched. Changing field or candidate aborts any request in flight and forgets the
 * previous answer.
 */
export function useExplain(fieldKey: string, candidateId: string): UseExplain {
  const id = `${fieldKey}|${candidateId}`
  const [state, setState] = useState<State>(() => empty(id))
  const ctrl = useRef<AbortController | null>(null)

  useEffect(() => () => ctrl.current?.abort(), [id])

  const run = useCallback(
    async (ai: boolean) => {
      ctrl.current?.abort()
      const c = new AbortController()
      ctrl.current = c
      const base = (s: State) => (s.id === id ? s : empty(id))
      setState((s) => ({ ...base(s), busy: ai ? 'ai' : 'facts', error: ai ? base(s).error : null, aiError: null }))
      try {
        const r = await fetchExplain(fieldKey, candidateId, ai, c.signal)
        if (c.signal.aborted) return
        setState((s) => {
          const prev = base(s)
          if (ai && !isReady(r)) {
            // the facts we already show stay; only the rewrite failed
            return { ...prev, busy: null, aiError: 'The AI summary is not available right now.' }
          }
          return { ...prev, response: r, busy: null, error: r.status === 'error' ? r.error : null }
        })
      } catch (e) {
        if (c.signal.aborted) return
        const message = (e as Error).message
        setState((s) => {
          const prev = base(s)
          return ai && isReady(prev.response)
            ? { ...prev, busy: null, aiError: message }
            : { ...prev, busy: null, error: message }
        })
      }
    },
    [fieldKey, candidateId, id],
  )

  const explain = useCallback(() => void run(false), [run])
  const rewrite = useCallback(() => void run(true), [run])

  const cur = state.id === id ? state : empty(id)
  const ready = isReady(cur.response) ? cur.response : null
  return {
    ready,
    processing: cur.response?.status === 'processing',
    busy: cur.busy,
    error: cur.error,
    ai: ready?.ai ?? null,
    aiError: cur.aiError ?? ready?.ai_error ?? null,
    explain,
    rewrite,
  }
}
