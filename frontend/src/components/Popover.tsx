import { useEffect, useId, useRef, useState, type ReactNode } from 'react'

interface Props {
  label: string
  /** Visible button content; defaults to the label. */
  button?: ReactNode
  buttonClass?: string
  title?: string
  children: ReactNode
  align?: 'left' | 'right'
  placement?: 'down' | 'up'
  width?: string
}

/** Small anchored popover: outside click and Escape close it, focus returns to the button. */
export function Popover({ label, button, buttonClass = 'tog', title, children, align = 'right', placement = 'down', width = 'w-[min(320px,calc(100vw-24px))]' }: Props) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const btn = useRef<HTMLButtonElement>(null)
  const id = useId()
  useEffect(() => {
    if (!open) return
    const down = (e: PointerEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false)
    }
    const key = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        setOpen(false)
        btn.current?.focus()
      }
    }
    document.addEventListener('pointerdown', down)
    document.addEventListener('keydown', key)
    return () => {
      document.removeEventListener('pointerdown', down)
      document.removeEventListener('keydown', key)
    }
  }, [open])
  return (
    <div ref={root} className="relative">
      <button
        ref={btn}
        type="button"
        className={buttonClass}
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        aria-label={button ? label : undefined}
        title={title ?? label}
        onClick={() => setOpen((o) => !o)}
      >
        {button ?? label}
      </button>
      {open && (
        <div
          id={id}
          role="dialog"
          aria-label={label}
          className={`popover ${width} ${align === 'right' ? 'right-0' : 'left-0'} ${placement === 'up' ? 'bottom-full mb-2' : 'top-full mt-2'}`}
        >
          {children}
        </div>
      )}
    </div>
  )
}
