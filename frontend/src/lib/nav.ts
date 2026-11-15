import type { MouseEvent } from 'react'

/** True for an ordinary left click, so modified clicks (new tab, new window) keep their native behaviour. */
export const isPlainClick = (e: MouseEvent) =>
  !e.defaultPrevented && e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey
