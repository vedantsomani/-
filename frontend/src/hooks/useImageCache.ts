import { useCallback, useEffect, useState } from 'react'
import { API_BASE } from '../lib/api'

/** Preload a set of image URLs so blinking between them never waits on the network.
 *  Returns a map url -> loaded <img>, a set of failed URLs, a retry method, and bumps `version` whenever more finish. */
export function useImageCache(urls: string[]) {
  const [cache] = useState(() => new Map<string, HTMLImageElement>())
  const [pending] = useState(() => new Set<string>())
  const [failed] = useState(() => new Set<string>())
  const [version, setVersion] = useState(0)
  const [retryTick, setRetryTick] = useState(0)

  useEffect(() => {
    let alive = true
    // Limit parallel requests so the first frames to show arrive first.
    const queue = urls.filter((u) => !cache.has(u) && !pending.has(u) && !failed.has(u))
    let active = 0
    const MAX = 8
    const next = () => {
      while (alive && active < MAX && queue.length) {
        const url = queue.shift()!
        pending.add(url)
        active++
        const img = new Image()
        img.decoding = 'async'
        // Cross-origin backend (VITE_API_BASE): request CORS so the canvas can still read pixels.
        if (API_BASE) img.crossOrigin = 'anonymous'
        img.onload = () => {
          cache.set(url, img)
          failed.delete(url)
          done(url)
        }
        img.onerror = () => {
          failed.add(url)
          done(url)
        }
        img.src = url
      }
    }
    const done = (url: string) => {
      pending.delete(url)
      active--
      setVersion((v) => v + 1)
      if (alive) next()
    }
    next()
    return () => {
      alive = false
    }
  }, [urls, retryTick, cache, pending, failed])

  const retry = useCallback((url: string) => {
    failed.delete(url)
    cache.delete(url)
    pending.delete(url)
    setRetryTick((t) => t + 1)
  }, [cache, pending, failed])

  return { images: cache, failed: failed, retry, version }
}
