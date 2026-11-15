import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { useModalFocus } from '../hooks/useModalFocus'
import { COMMAND_HELP, parseCommands } from '../lib/commands'
import type { ViewState } from '../lib/viewState'
import { IconArrowRight, IconSearch } from './icons'

export interface PaletteAction {
  id: string
  group: string
  label: string
  /** Extra words the filter matches (e.g. "d6", "gaia"). */
  keywords?: string
  /** Right-aligned hint: a shortcut or the current state. */
  hint?: ReactNode
  run: () => void
}

interface Props {
  onClose: () => void
  actions: PaletteAction[]
  /** The current view, so typed display commands ("d6 difference gaia on") can be applied. Null on the landing page. */
  view: ViewState | null
  update: (patch: Partial<ViewState>) => void
}

/** Command palette (Ctrl/Cmd+K or /): filter app actions, or type display commands from lib/commands.ts. */
export function CommandPalette({ onClose, actions, view, update }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const id = useId()
  const [text, setText] = useState('')
  const [active, setActive] = useState(0)
  const [feedback, setFeedback] = useState<{ applied: string[]; messages: string[] } | null>(null)
  useModalFocus(ref, true, onClose)

  const q = text.trim().toLowerCase()
  // A line that parses as display commands becomes the first entry.
  const parsed = useMemo(() => (view && q ? parseCommands(q, view) : null), [q, view])
  const commandItem: PaletteAction | null =
    parsed && parsed.applied.length && view
      ? {
          id: 'typed',
          group: 'Display command',
          label: `Apply: ${parsed.applied.join(', ')}`,
          run: () => {
            update(parsed.patch)
          },
        }
      : null
  const filtered = useMemo(() => {
    if (!q) return actions
    const words = q.split(/\s+/)
    return actions.filter((a) => {
      const hay = `${a.group} ${a.label} ${a.keywords ?? ''}`.toLowerCase()
      return words.every((w) => hay.includes(w))
    })
  }, [actions, q])
  const items = commandItem ? [commandItem, ...filtered] : filtered
  const safe = Math.min(active, Math.max(items.length - 1, 0))

  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-i="${safe}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [safe])

  const runItem = (a: PaletteAction) => {
    a.run()
    onClose()
  }
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActive((i) => Math.min(i + 1, items.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActive((i) => Math.max(i - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const a = items[safe]
      if (a) runItem(a)
      else if (view && q) {
        const r = parseCommands(q, view)
        if (Object.keys(r.patch).length) update(r.patch)
        setFeedback({ applied: r.applied, messages: r.messages })
      }
    }
  }

  return (
    <div className="palette-backdrop" onPointerDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={ref} className="palette" role="dialog" aria-modal="true" aria-label="Command palette">
        <div className="palette-input">
          <IconSearch />
          <input
            value={text}
            onChange={(e) => {
              setText(e.target.value)
              setActive(0)
              setFeedback(null)
            }}
            onKeyDown={onKey}
            placeholder={view ? 'Search actions, or type a command such as “d6 difference gaia on”' : 'Search actions and examples'}
            aria-label="Command"
            role="combobox"
            aria-expanded="true"
            aria-controls={`${id}-list`}
            aria-activedescendant={items[safe] ? `${id}-${safe}` : undefined}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
          />
          <span className="kbd">Esc</span>
        </div>
        <div ref={listRef} id={`${id}-list`} role="listbox" className="palette-list" aria-label="Actions">
          {items.length === 0 && (
            <p className="px-3 py-4 text-ink-300">
              {view ? (
                <>
                  Nothing matches. Display commands: <span className="mono text-ink-400">{COMMAND_HELP}</span>
                </>
              ) : (
                'Nothing matches.'
              )}
            </p>
          )}
          {items.map((a, i) => {
            const head = i === 0 || items[i - 1].group !== a.group ? a.group : null
            return (
              <div key={a.id}>
                {head && <div className="palette-group">{head}</div>}
                <button
                  type="button"
                  id={`${id}-${i}`}
                  data-i={i}
                  role="option"
                  aria-selected={i === safe}
                  className="palette-item"
                  tabIndex={-1}
                  onPointerMove={() => setActive(i)}
                  onClick={() => runItem(a)}
                >
                  <IconArrowRight width={14} height={14} />
                  <span>{a.label}</span>
                  {a.hint && <span className="hint">{a.hint}</span>}
                </button>
              </div>
            )
          })}
        </div>
        {feedback && (feedback.messages.length > 0 || feedback.applied.length > 0) && (
          <div className="palette-feedback" role="status">
            {feedback.messages.map((m) => (
              <p key={m} style={{ color: 'var(--variable)' }}>
                {m}
              </p>
            ))}
            {feedback.applied.length > 0 && <p className="text-ink-300">Applied: {feedback.applied.join(', ')}.</p>}
          </div>
        )}
        <div className="palette-foot">
          <span>
            <span className="kbd">↑</span> <span className="kbd">↓</span> move
          </span>
          <span>
            <span className="kbd">Enter</span> run
          </span>
          {view && <span className="hide-sm">Type “help” for every display command</span>}
        </div>
      </div>
    </div>
  )
}
