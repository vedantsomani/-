import { useRef } from 'react'

import { useModalFocus } from '../hooks/useModalFocus'

interface Props {
  open: boolean
  onClose: () => void
}

interface ShortcutItem {
  key: string
  label: string
  action: string
}

const SHORTCUTS: readonly ShortcutItem[] = [
  { key: 'Space', label: 'Space', action: 'Play or pause the blink' },
  { key: '← / →', label: 'Left / Right', action: 'Previous or next image' },
  { key: '↑ / ↓', label: 'Up / Down', action: 'Next or previous detector (D1 to D6)' },
  { key: 'D', label: 'D', action: 'Next image type (Seasons, Visits, λ-matched, Difference, Exposures, Colour)' },
  { key: 'I', label: 'I', action: 'Invert the image' },
  { key: 'F', label: 'F', action: 'Full screen image' },
  { key: 'Y / N / U', label: 'Y / N / U', action: 'Your review of a candidate: yes, no, unsure' },
  { key: '+ / -', label: '+ / -', action: 'Faster or slower blink' },
  { key: '/ or Ctrl K', label: 'Slash or Control K', action: 'Open the command palette (Cmd K on a Mac)' },
  { key: '?', label: '?', action: 'Toggle this keyboard shortcuts sheet' },
  { key: 'Esc', label: 'Esc', action: 'Close dialogs or exit fullscreen' },
]

export function KeyboardShortcutsModal({ open, onClose }: Props) {
  const dialogRef = useRef<HTMLDivElement>(null)

  useModalFocus(dialogRef, open, onClose)


  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="shortcuts-title"
      onClick={onClose}
    >
      <div
        ref={dialogRef}
        className="relative w-full max-w-lg rounded-xl border border-line bg-ink-900 p-6 shadow-[0_24px_64px_rgb(0_0_0/0.55)]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-ink-700 pb-4">
          <div className="flex items-center gap-2">
            <span className="flex h-7 w-7 items-center justify-center rounded-md bg-[#e8ff00] text-ink-950 font-bold text-sm">
              ?
            </span>
            <h2 id="shortcuts-title" className="font-heading text-lg font-bold text-white">
              Keyboard Navigation Shortcuts
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-1.5 text-ink-500 hover:bg-ink-800 hover:text-white"
            aria-label="Close shortcuts modal"
          >
            ✕
          </button>
        </div>

        <div className="mt-4 flex flex-col divide-y divide-ink-800 text-sm">
          {SHORTCUTS.map((s) => (
            <div key={s.key} className="flex items-center justify-between py-2.5">
              <span className="text-ink-300">{s.action}</span>
              <kbd className="inline-flex min-w-8 items-center justify-center rounded-md border border-line bg-ink-950 px-2.5 py-1 font-mono text-xs font-semibold text-slate-100 shadow-xs">
                {s.label}
              </kbd>
            </div>
          ))}
        </div>

        <div className="mt-6 flex items-center justify-between border-t border-ink-800 pt-4 text-xs text-ink-500">
          <span>SPHEREx Time Machine · Observatory Mode</span>
          <button
            type="button"
            onClick={onClose}
            className="btn btn-sm btn-primary"
          >
            Got it
          </button>
        </div>
      </div>
    </div>
  )
}
