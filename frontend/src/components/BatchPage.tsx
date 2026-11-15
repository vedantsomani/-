import { useState, type ChangeEvent } from 'react'
import { useBatch } from '../hooks/useBatch'
import { apiUrl } from '../lib/api'
import {
  DEFAULT_SORT,
  EPOCHS,
  EXAMPLE_LIST,
  countTargets,
  createBatch,
  downloadUrl,
  firstDir,
  fmt,
  fmtConfidence,
  fmtFlux,
  sortRows,
  type BatchRow,
  type Sort,
  type SortKey,
} from '../lib/batch'
import { FOV_OPTIONS } from '../lib/constants'
import { isPlainClick } from '../lib/nav'

interface Props {
  /** `new` shows the form; anything else is a batch id. */
  id: string
  onStarted: (id: string) => void
  onHome: () => void
  /** Open a target in the viewer (`?ra=..&dec=..`). */
  onOpenView: (search: string) => void
}

export function BatchPage({ id, onStarted, onHome, onOpenView }: Props) {
  return (
    <div className="flex flex-col gap-4">
      <nav aria-label="Breadcrumb" className="text-sm text-ink-300">
        <a
          href="/"
          onClick={(e) => {
            if (!isPlainClick(e)) return
            e.preventDefault()
            onHome()
          }}
          className="hover:text-accent"
        >
          Home
        </a>
        <span aria-hidden="true"> / </span>
        <span className="text-slate-100">Check a list of targets</span>
      </nav>
      {id === 'new' ? <NewBatch onStarted={onStarted} /> : <Results id={id} onOpenView={onOpenView} />}
    </div>
  )
}

// ---- new batch --------------------------------------------------------------------------------

function NewBatch({ onStarted }: { onStarted: (id: string) => void }) {
  const [text, setText] = useState('')
  const [fov, setFov] = useState(5)
  const [epoch, setEpoch] = useState(EPOCHS[0].mjd)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const n = countTargets(text)

  const onFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    try {
      setText(await file.text())
      setError(null)
    } catch {
      setError('Could not read that file.')
    }
  }

  const start = async () => {
    setBusy(true)
    setError(null)
    try {
      onStarted((await createBatch(text, fov, epoch)).id)
    } catch (e) {
      setError((e as Error).message)
      setBusy(false)
    }
  }

  return (
    <section aria-labelledby="batch-title" className="card flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <h2 id="batch-title" className="text-xl font-semibold text-white">
          Check a list of targets
        </h2>
        <p className="text-sm leading-relaxed text-ink-300">
          Paste or upload one target per line. The server downloads the SPHEREx images of each one, measures it and ranks
          the list, so you can open the most interesting ones first. Fields already downloaded finish in seconds; new ones
          take a few minutes each.
        </p>
      </div>

      <div className="flex flex-col gap-1.5">
        <label htmlFor="batch-text" className="card-title">
          Targets
        </label>
        <textarea
          id="batch-text"
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={10}
          spellCheck={false}
          placeholder={'133.76002 -7.24221 -8123.7 673.2 # WISE 0855\n76.95653 30.40144 # RW Aur\nBarnard\'s Star'}
          className="input min-h-48 w-full resize-y font-mono leading-relaxed"
        />
        <p className="help">
          Each line: <code>ra dec</code> in degrees or <code>08:55:10.8 -07:14:42</code>, optionally followed by{' '}
          <code>pmra pmdec</code> in mas/yr and a label after <code>#</code> (or as extra comma-separated columns). A line
          with just an object name is looked up in SIMBAD. A CSV with the header <code>ra,dec,pmra,pmdec,name</code> also
          works. Lines starting with <code>#</code> are ignored.
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-x-4 gap-y-3">
        <button type="button" className="btn btn-sm" onClick={() => setText(EXAMPLE_LIST)}>
          Load the 5 example targets
        </button>
        <label className="btn btn-sm cursor-pointer">
          Upload .txt or .csv
          <input type="file" accept=".txt,.csv,.tsv,text/plain,text/csv" className="sr-only" onChange={onFile} />
        </label>
        <div className="flex flex-col gap-1">
          <label htmlFor="batch-fov" className="help">
            Field of view
          </label>
          <select id="batch-fov" value={fov} onChange={(e) => setFov(Number(e.target.value))} className="input w-20 px-2">
            {FOV_OPTIONS.map((f) => (
              <option key={f} value={f}>
                {f}′
              </option>
            ))}
          </select>
        </div>
        <div className="flex min-w-0 flex-col gap-1">
          <label htmlFor="batch-epoch" className="help">
            Positions with a proper motion are at
          </label>
          <select id="batch-epoch" value={epoch} onChange={(e) => setEpoch(Number(e.target.value))} className="input px-2">
            {EPOCHS.map((o) => (
              <option key={o.mjd} value={o.mjd}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button type="button" className="btn btn-primary" disabled={busy || n === 0} onClick={start}>
          {busy ? 'Starting…' : `Start${n ? ` (${n} target${n === 1 ? '' : 's'})` : ''}`}
        </button>
        {error && (
          <p role="alert" className="text-sm text-red-300">
            {error}
          </p>
        )}
      </div>
    </section>
  )
}

// ---- results ----------------------------------------------------------------------------------

const COLUMNS: { key: SortKey | null; title: string; className?: string }[] = [
  { key: 'i', title: '#' },
  { key: null, title: 'Preview' },
  { key: 'label', title: 'Target' },
  { key: 'verdict', title: 'Verdict' },
  { key: 'class_label', title: 'Class' },
  { key: 'ch4_sigma', title: 'Methane index' },
  { key: 'flux_4p5_ujy', title: '4.5 µm flux' },
  { key: 'blend_warning', title: 'Blend' },
  { key: 'status', title: 'Status' },
]

function Results({ id, onOpenView }: { id: string; onOpenView: (search: string) => void }) {
  const { data, error, cancel } = useBatch(id)
  const [sort, setSort] = useState<Sort>(DEFAULT_SORT)
  const [copied, setCopied] = useState<'yes' | 'no' | null>(null)

  const copyLink = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href)
      setCopied('yes')
    } catch {
      setCopied('no')
    }
    window.setTimeout(() => setCopied(null), 2500)
  }

  const clickHeader = (key: SortKey) =>
    setSort((s) => (s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: firstDir(key) }))

  if (!data) {
    return error ? (
      <p role="alert" className="card text-sm text-red-300">
        {error}
      </p>
    ) : (
      <p className="card text-sm text-ink-300">Loading the list…</p>
    )
  }

  const running = data.status === 'running'
  const pct = data.total ? Math.round((100 * data.done) / data.total) : 100
  const rows = sortRows(data.rows, sort)

  return (
    <section aria-labelledby="batch-title" className="flex flex-col gap-4">
      <div className="card flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
          <h2 id="batch-title" className="text-xl font-semibold text-white">
            {running ? 'Checking your targets' : data.cancelled ? 'Stopped' : 'Done'}
            <span className="ml-2 text-sm font-normal text-ink-300">
              {data.done} of {data.total} · {data.fov}′ fields
            </span>
          </h2>
          <div className="flex flex-wrap items-center gap-2">
            <a className="btn btn-sm" href={downloadUrl(id, 'csv')} download>
              Download CSV
            </a>
            <a className="btn btn-sm" href={downloadUrl(id, 'json')} download>
              Download JSON
            </a>
            <button type="button" className="btn btn-sm" onClick={copyLink}>
              {copied === 'yes' ? 'Copied' : copied === 'no' ? 'Copy failed: use the address bar' : 'Copy link'}
            </button>
            {running && (
              <button type="button" className="btn btn-sm btn-quiet" onClick={() => void cancel()}>
                Stop
              </button>
            )}
          </div>
        </div>
        <div
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={data.total}
          aria-valuenow={data.done}
          aria-label="Targets checked"
          className="h-2 overflow-hidden rounded-full bg-ink-800"
        >
          <div className="h-full bg-accent transition-[width] duration-500" style={{ width: `${pct}%` }} />
        </div>
        <p className="help">
          Sorted by {sort.key === 'ch4_sigma' && sort.dir === 'desc' ? 'strongest methane index, then 4.5 µm S/N' : 'your choice'}.
          Click a column heading to sort. Open a target to see its images and spectrum. {running && 'This page updates by itself.'}
        </p>
        {error && (
          <p role="alert" className="text-sm text-amber-300">
            Connection problem, retrying: {error}
          </p>
        )}
      </div>

      <div className="card overflow-x-auto p-0 sm:p-0">
        <table className="w-full min-w-[56rem] border-collapse text-left text-sm">
          <caption className="sr-only">Targets ranked by measurement</caption>
          <thead>
            <tr className="border-b border-ink-700 text-xs text-ink-300">
              {COLUMNS.map((c) => {
                const active = c.key !== null && sort.key === c.key
                return (
                  <th
                    key={c.title}
                    scope="col"
                    aria-sort={active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : undefined}
                    className="px-3 py-2 font-medium whitespace-nowrap"
                  >
                    {c.key === null ? (
                      c.title
                    ) : (
                      <button
                        type="button"
                        onClick={() => clickHeader(c.key as SortKey)}
                        className={`inline-flex items-center gap-1 rounded hover:text-white ${active ? 'text-accent' : ''}`}
                      >
                        {c.title}
                        <span aria-hidden="true">{active ? (sort.dir === 'asc' ? '▲' : '▼') : ''}</span>
                      </button>
                    )}
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <Row key={r.i} r={r} onOpenView={onOpenView} />
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function Thumb({ src }: { src: string | null }) {
  const [bad, setBad] = useState(false)
  const box = 'h-14 w-14 shrink-0 rounded border border-ink-700 bg-ink-950'
  if (!src || bad) return <div aria-hidden="true" className={box} />
  return <img src={apiUrl(src)} alt="" loading="lazy" onError={() => setBad(true)} className={`pixelated object-cover ${box}`} />
}

const EVIDENCE_CLASS: Record<string, string> = {
  strong: 'border-emerald-500/50 text-emerald-300',
  moderate: 'border-amber-500/50 text-amber-300',
  weak: 'border-ink-600 text-ink-300',
}

function statusText(r: BatchRow): { text: string; tone: string } {
  switch (r.status) {
    case 'queued':
      return { text: 'Queued', tone: 'text-ink-500' }
    case 'building':
      if (r.progress && r.progress[1] > 0) return { text: `Downloading ${r.progress[0]} of ${r.progress[1]}`, tone: 'text-accent' }
      return { text: r.stage === 'resolving' ? 'Looking up name' : 'Building field', tone: 'text-accent' }
    case 'measuring':
      return { text: r.stage ? `Measuring (${r.stage.replace('fingerprint: ', '')})` : 'Measuring', tone: 'text-accent' }
    case 'error':
      return { text: r.error ?? 'Failed', tone: 'text-red-300' }
    case 'complete':
      return r.error ? { text: r.error, tone: 'text-amber-300' } : { text: 'Done', tone: 'text-ink-300' }
  }
}

function Row({ r, onOpenView }: { r: BatchRow; onOpenView: (search: string) => void }) {
  const status = statusText(r)
  const pm = r.measured_pm_masyr
  return (
    <tr className="border-b border-ink-800 align-top last:border-0">
      <td className="px-3 py-2 text-ink-500">{r.i}</td>
      <td className="px-3 py-2">
        <Thumb key={r.thumb_url ?? ''} src={r.thumb_url} />
      </td>
      <td className="max-w-56 px-3 py-2">
        {r.view_url ? (
          <a
            href={r.view_url}
            onClick={(e) => {
              if (!isPlainClick(e) || !r.view_url) return
              e.preventDefault()
              onOpenView(r.view_url)
            }}
            className="font-medium break-words text-slate-100 underline decoration-ink-600 underline-offset-2 hover:text-accent"
          >
            {r.label}
          </a>
        ) : (
          <span className="font-medium break-words text-slate-100">{r.label || r.input}</span>
        )}
        {r.ra !== null && r.dec !== null && (
          <div className="help font-mono">
            {r.ra.toFixed(4)} {r.dec >= 0 ? '+' : ''}
            {r.dec.toFixed(4)}
          </div>
        )}
      </td>
      <td className="px-3 py-2">
        {r.verdict ? (
          <div className="flex flex-col items-start gap-1">
            <span className="text-slate-100 capitalize">{r.verdict}</span>
            {r.evidence && (
              <span className={`chip ${EVIDENCE_CLASS[r.evidence] ?? ''}`}>{r.evidence} evidence</span>
            )}
            {pm && (
              <span className="help">
                measured {fmt(pm[0], 0)}, {fmt(pm[1], 0)} mas/yr
              </span>
            )}
          </div>
        ) : (
          <span className="text-ink-500">–</span>
        )}
      </td>
      <td className="px-3 py-2">
        {r.class_label ? (
          <>
            <span className="text-slate-100">{r.class_label}</span>
            {r.class_confidence !== null && <div className="help">{fmtConfidence(r.class_confidence)}</div>}
          </>
        ) : (
          <span className="text-ink-500">–</span>
        )}
      </td>
      <td className="px-3 py-2 whitespace-nowrap tabular-nums">
        {r.ch4_index !== null ? (
          <>
            <span className="text-slate-100">{fmt(r.ch4_index, 2)}</span>
            {r.ch4_sigma !== null && <div className="help">{fmt(r.ch4_sigma, 1)}σ</div>}
          </>
        ) : (
          <span className="text-ink-500">–</span>
        )}
      </td>
      <td className="px-3 py-2 whitespace-nowrap tabular-nums">
        {r.flux_4p5_ujy !== null ? (
          <>
            <span className="text-slate-100">{fmtFlux(r.flux_4p5_ujy, r.flux_err_4p5_ujy)}</span>
            <div className="help">S/N {fmt(r.snr_4p5, 1)}</div>
          </>
        ) : (
          <span className="text-ink-500">–</span>
        )}
      </td>
      <td className="px-3 py-2">
        {r.blend_warning ? (
          <span className="chip border-amber-500/50 text-amber-300" title="A neighbouring star overlaps this target. The spectrum is deblended; check it before trusting it.">
            blend
          </span>
        ) : (
          <span className="text-ink-500">{r.blend_warning === false ? 'no' : '–'}</span>
        )}
      </td>
      <td className={`max-w-56 px-3 py-2 text-xs break-words ${status.tone}`}>{status.text}</td>
    </tr>
  )
}
