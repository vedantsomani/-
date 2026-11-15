import { useEffect, useState } from 'react'
import { fetchField, type FieldResponse } from '../lib/api'

const POLL_MS = 2000

/** Request a field and poll until the backend finishes building it. */
export function useField(target: { ra: number; dec: number } | null, fov: number) {
  const [field, setField] = useState<FieldResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setField(null)
    setError(null)
    if (!target) return
    const ctrl = new AbortController()
    let timer: number | undefined

    const tick = async () => {
      try {
        const f = await fetchField(target.ra, target.dec, fov, ctrl.signal)
        setField(f)
        if (f.status === 'error') setError(f.error ?? 'field build failed')
        else if (f.status !== 'complete') timer = window.setTimeout(tick, POLL_MS)
      } catch (e) {
        if (ctrl.signal.aborted) return
        setError((e as Error).message)
      }
    }
    tick()
    return () => {
      ctrl.abort()
      window.clearTimeout(timer)
    }
  }, [target?.ra, target?.dec, fov]) // eslint-disable-line react-hooks/exhaustive-deps

  return { field, error }
}
