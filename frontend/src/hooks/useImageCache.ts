import { useCallback, useEffect, useState } from 'react'

async function loadImage(url: string, signal: AbortSignal, reload: boolean) {
  const requestSignal = AbortSignal.any([signal, AbortSignal.timeout(30_000)])
  const response = await fetch(url, {
    signal: requestSignal,
    cache: reload ? 'reload' : 'default',
  })
  if (!response.ok) throw new Error(`Image request returned HTTP ${response.status}`)

  const blob = await response.blob()
  if (!blob.type.startsWith('image/')) throw new Error('Image request returned a non-image response')
  signal.throwIfAborted()

  const objectUrl = URL.createObjectURL(blob)
  try {
    const image = new Image()
    image.src = objectUrl
    await image.decode()
    return image
  } finally {
    URL.revokeObjectURL(objectUrl)
  }
}

/** Preload decoded images so blinking between them does not wait on the network. */
export function useImageCache(urls: string[]) {
  const [cache] = useState(() => new Map<string, HTMLImageElement>())
  const [failed] = useState(() => new Set<string>())
  const [reloadUrls] = useState(() => new Set<string>())
  const [version, setVersion] = useState(0)
  const [retryTick, setRetryTick] = useState(0)
  // Field polling can rebuild the array even when the sequence is unchanged.
  const sequence = JSON.stringify(urls)

  useEffect(() => {
    const controller = new AbortController()
    const { signal } = controller
    const queue = [...new Set<string>(JSON.parse(sequence))].filter(
      (url) => !cache.has(url) && !failed.has(url),
    )
    let nextIndex = 0

    async function preload() {
      while (!signal.aborted && nextIndex < queue.length) {
        const url = queue[nextIndex++]
        try {
          const image = await loadImage(url, signal, reloadUrls.has(url))
          if (signal.aborted) return
          cache.set(url, image)
        } catch {
          if (signal.aborted) return
          failed.add(url)
        }
        reloadUrls.delete(url)
        setVersion((value) => value + 1)
      }
    }

    for (let worker = 0; worker < Math.min(8, queue.length); worker++) void preload()
    return () => controller.abort()
  }, [sequence, retryTick, cache, failed, reloadUrls])

  const retry = useCallback((url: string) => {
    failed.delete(url)
    cache.delete(url)
    reloadUrls.add(url)
    setRetryTick((tick) => tick + 1)
  }, [cache, failed, reloadUrls])

  return { images: cache, failed, retry, version }
}
