import { useEffect, useId, useRef, useState } from 'react'
import { EXAMPLE_TARGETS } from '../lib/constants'
import { isPlainClick } from '../lib/nav'
import { IconChevronDown } from './icons'

interface Props {
  /** Open one of the cached example views (the exact `?ra=…` search string). */
  onOpen: (search: string) => void
}

/** Top-bar menu listing the example targets with a real SPHEREx thumbnail of each. */
export function ExamplesMenu({ onOpen }: Props) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const panelId = useId()

  useEffect(() => {
    if (!open) return
    const onDown = (e: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setOpen(false)
        buttonRef.current?.focus()
      }
    }
    document.addEventListener('pointerdown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div ref={rootRef} className="relative">
      <button
        ref={buttonRef}
        type="button"
        className="btn btn-ghost btn-sm"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={() => setOpen((o) => !o)}
      >
        Examples
        <IconChevronDown className={`transition-transform duration-150 ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div id={panelId} role="region" aria-label="Example targets" className="popover right-0 mt-2 w-[min(23rem,calc(100vw-1.5rem))] p-1.5">
          <ul className="flex flex-col">
            {EXAMPLE_TARGETS.map((ex) => (
              <li key={ex.id}>
                <a
                  href={ex.search}
                  onClick={(e) => {
                    if (!isPlainClick(e)) return
                    e.preventDefault()
                    setOpen(false)
                    onOpen(ex.search)
                  }}
                  className="flex items-center gap-3 rounded-md px-2 py-2 hover:bg-ink-800"
                >
                  <img src={`/examples/${ex.id}.png`} alt="" width={40} height={40} className="pixelated h-10 w-10 shrink-0 rounded bg-black" />
                  <span className="flex min-w-0 flex-col">
                    <span className="text-[13px] font-semibold">{ex.name}</span>
                    <span className="text-xs text-ink-400">
                      {ex.tag} · <span className="tabular">{ex.motionPerYear}</span>
                    </span>
                  </span>
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
