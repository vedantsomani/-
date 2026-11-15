import { useEffect, useRef, useState } from 'react'

/** Track an element's content width (px) so SVG charts can use real pixel units. */
export function useElementWidth<T extends HTMLElement>(initial = 640) {
  const ref = useRef<T | null>(null)
  const [width, setWidth] = useState(initial)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    setWidth(Math.round(el.getBoundingClientRect().width) || initial)
    const ro = new ResizeObserver(([entry]) => {
      const w = Math.round(entry.contentRect.width)
      if (w > 0) setWidth(w)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [initial])

  return [ref, width] as const
}
