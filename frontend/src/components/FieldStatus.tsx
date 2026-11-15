import { useEffect, useState } from 'react'
import type { FieldResponse } from '../lib/api'
import { EXAMPLE_TARGETS } from '../lib/constants'
import { isPlainClick } from '../lib/nav'
import { relativeTime } from '../lib/fieldPhase'
import { SEARCH_INPUT_ID } from './SearchBar'

type Retry = (opts?: { rebuild?: boolean; update?: boolean }) => void | Promise<number>

function explain(msg: string): { title: string; hint: string } {
  if (/failed to fetch|networkerror|load failed|network request failed/i.test(msg)) {
    return { title: 'Can’t reach the GAMAS server.', hint: 'Check your connection, then retry.' }
  }
  if (/50[234]|bad gateway|unavailable|gateway time|internal server error|econnrefused|proxy/i.test(msg)) {
    return { title: 'The server isn’t responding.', hint: 'It may be restarting or busy. Retry in a few seconds.' }
  }
  return {
    title: 'SPHEREx images for this position could not be loaded.',
    hint: 'NASA’s IRSA archive may be busy. Anything already downloaded is kept, so a retry is quick.',
  }
}

const Alert = ({ tone, children }: { tone: 'error' | 'warn' | 'info'; children: React.ReactNode }) => (
  <div
    role={tone === 'info' ? undefined : 'alert'}
    className={`flex flex-col gap-2 rounded-lg border p-3 text-[13px] ${
      tone === 'error'
        ? 'border-[rgb(255_123_114/0.4)] bg-[rgb(255_123_114/0.08)]'
        : tone === 'warn'
          ? 'border-[rgb(242_184_75/0.4)] bg-[rgb(242_184_75/0.08)]'
          : 'border-line bg-ink-900'
    }`}
  >
    {children}
  </div>
)

interface AlertsProps {
  field: FieldResponse | null
  error: string | null
  onRetry: Retry
  onOpenExample: (search: string) => void
}

/** Errors and empty states that replace or precede the image. Nothing renders while all is well. */
export function FieldAlerts({ field, error, onRetry, onOpenExample }: AlertsProps) {
  const nFailed = field ? Object.keys(field.failed).length : 0
  const complete = !!field && field.status === 'complete'
  const noCoverage = complete && field.total === 0
  const allFailed = complete && field.total > 0 && field.frames.length === 0 && nFailed > 0
  const e = error ? explain(error) : null

  return (
    <>
      {e && (
        <Alert tone="error">
          <p className="font-semibold">{e.title}</p>
          <p className="text-ink-300">{e.hint}</p>
          <div className="flex flex-wrap items-center gap-3">
            <button type="button" className="btn btn-primary btn-sm" onClick={() => onRetry()}>
              Retry
            </button>
            <details className="text-xs text-ink-300">
              <summary>Technical details</summary>
              <p className="mt-1 font-mono break-all">{error}</p>
            </details>
          </div>
        </Alert>
      )}
      {noCoverage && (
        <Alert tone="info">
          <p className="font-semibold">No SPHEREx images cover this position yet.</p>
          <p className="text-ink-300">
            SPHEREx is still mapping the sky and IRSA publishes images in batches. Try a nearby position, a wider field
            of view, or an example.
          </p>
          <p className="help">Searched: {field.collections.join(', ')}</p>
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" className="btn btn-primary btn-sm" onClick={() => document.getElementById(SEARCH_INPUT_ID)?.focus()}>
              Try another position
            </button>
            {EXAMPLE_TARGETS.slice(0, 3).map((ex) => (
              <a
                key={ex.id}
                href={ex.search}
                className="btn btn-sm"
                onClick={(ev) => {
                  if (!isPlainClick(ev)) return
                  ev.preventDefault()
                  onOpenExample(ex.search)
                }}
              >
                {ex.name}
              </a>
            ))}
          </div>
        </Alert>
      )}
      {allFailed && (
        <Alert tone="error">
          <p className="font-semibold">None of the {field.total} exposures could be downloaded.</p>
          <p className="text-ink-300">IRSA is probably busy or unreachable. Retrying asks the server to start this position again.</p>
          <div className="flex flex-wrap items-center gap-3">
            <button type="button" className="btn btn-primary btn-sm" onClick={() => onRetry({ rebuild: true })}>
              Retry
            </button>
            <details className="text-xs text-ink-300">
              <summary>Technical details</summary>
              <ul className="mt-1 font-mono">
                {Object.entries(field.failed)
                  .slice(0, 6)
                  .map(([k, v]) => (
                    <li key={k}>
                      {k}: {v}
                    </li>
                  ))}
              </ul>
            </details>
          </div>
        </Alert>
      )}
    </>
  )
}

/** Data provenance line for the inspector: counts, partial downloads, last archive check and the re-check button. */
export function DataFreshness({ field, onRetry }: { field: FieldResponse; onRetry: Retry }) {
  const nFailed = Object.keys(field.failed).length
  const partial = field.status === 'complete' && field.frames.length > 0 && nFailed > 0
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 30_000)
    return () => window.clearInterval(t)
  }, [])
  const [check, setCheck] = useState<{ busy: boolean; key?: string; found?: number; err?: string }>({ busy: false })
  async function checkForNew() {
    setCheck({ busy: true, key: field.key })
    try {
      const found = await onRetry({ update: true })
      setCheck({ busy: false, key: field.key, found: typeof found === 'number' ? found : 0 })
    } catch (ex) {
      setCheck({ busy: false, key: field.key, err: (ex as Error).message })
    }
  }
  const checked = check.key === field.key ? check : null
  if (field.status !== 'complete' || field.total === 0) return null

  return (
    <div className="flex flex-col gap-2 text-xs text-ink-300">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span title={field.last_checked ?? undefined}>
          {field.frames.length} exposures · {field.collections.join(', ')} ·{' '}
          {field.last_checked ? `archive checked ${relativeTime(field.last_checked, now)}` : 'not re-checked yet'}
        </span>
        <button type="button" className="btn btn-sm" disabled={!!checked?.busy} onClick={checkForNew} data-check-new>
          {checked?.busy ? 'Checking…' : 'Check for new observations'}
        </button>
      </div>
      <span role="status" className={checked?.err ? 'text-danger' : 'text-ink-300'}>
        {checked?.err
          ? `Could not reach the archive: ${checked.err}`
          : checked && !checked.busy && checked.found === 0
            ? 'No new observations.'
            : checked && !checked.busy && checked.found
              ? `${checked.found} new exposure${checked.found === 1 ? '' : 's'} added.`
              : ''}
      </span>
      {partial && (
        <details className="text-ink-300">
          <summary className="cursor-pointer">
            {nFailed} of {field.total} exposures could not be loaded and are left out
          </summary>
          <ul className="mt-1 font-mono">
            {Object.entries(field.failed).map(([k, v]) => (
              <li key={k}>
                {k}: {v}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}
