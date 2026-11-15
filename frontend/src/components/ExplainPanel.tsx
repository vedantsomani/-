import { useId } from 'react'
import { detectorColor } from '../lib/constants'
import {
  EXPLAIN_SECTIONS,
  refLabel,
  type ExplainFacts,
  type ExplainItem,
  type ExplainReady,
  type ExplainRef,
} from '../lib/explain'
import { useExplain } from '../hooks/useExplain'

const BUTTON = 'btn btn-sm'
const PRIMARY = 'btn btn-primary'
const CHIP =
  'inline-flex items-center gap-1.5 rounded-full border border-ink-600 bg-ink-800 px-2 py-0.5 text-[11px] text-ink-300'

const EVIDENCE_STYLE = {
  strong: 'text-green-300',
  moderate: 'text-amber-200',
  weak: 'text-ink-500',
} as const

interface Props {
  /** `key` of the field (FieldResponse.key). */
  fieldKey: string
  /** "target" or a candidate id such as "c3". */
  candidateId: string
  /** Optional: makes the reference chips clickable (e.g. open that difference view). */
  onSelectRef?: (ref: ExplainRef) => void
}

function RefChip({ r, facts, onSelect }: { r: ExplainRef; facts: ExplainFacts; onSelect?: (ref: ExplainRef) => void }) {
  const label = refLabel(r, facts)
  const body = (
    <>
      <span aria-hidden className="h-2 w-2 rounded-full" style={{ backgroundColor: detectorColor(r.detector) }} />
      {label}
    </>
  )
  if (!onSelect) {
    return (
      <span className={CHIP} title="Detector and pair of seasons this sentence is based on">
        {body}
      </span>
    )
  }
  return (
    <button
      type="button"
      onClick={() => onSelect(r)}
      className={`${CHIP} hover:border-accent hover:text-slate-100 focus-visible:outline-2 focus-visible:outline-accent`}
      title="Show this comparison"
      aria-label={`Show ${label}`}
    >
      {body}
    </button>
  )
}

function Sentences({
  items,
  facts,
  onSelectRef,
}: {
  items: ExplainItem[]
  facts: ExplainFacts
  onSelectRef?: (ref: ExplainRef) => void
}) {
  return (
    <ul className="flex flex-col gap-2">
      {items.map((it, i) => (
        <li key={i} className="flex flex-col gap-1">
          <p className="text-sm leading-relaxed text-slate-200">{it.text}</p>
          {it.refs.length > 0 && (
            <div className="flex flex-wrap gap-1" aria-label="Measurements behind this sentence">
              {it.refs.map((r) => (
                <RefChip key={`${r.detector}-${r.season_a}-${r.season_b}`} r={r} facts={facts} onSelect={onSelectRef} />
              ))}
            </div>
          )}
        </li>
      ))}
    </ul>
  )
}

function AiBox({ text, model }: { text: string; model: string }) {
  return (
    <aside aria-label="AI-written summary" className="rounded-md border border-line bg-ink-950 p-3">
      <p className="text-xs font-medium text-ink-400">
        AI-written summary of the measurements above; every number is checked against them
      </p>
      <p className="mt-1.5 text-sm leading-relaxed whitespace-pre-line text-slate-200">{text}</p>
      <p className="mt-1.5 text-[11px] text-ink-500">
        Written by <span className="font-mono">{model}</span>
      </p>
    </aside>
  )
}

function Result({
  data,
  ai,
  aiError,
  busy,
  onRewrite,
  onSelectRef,
}: {
  data: ExplainReady
  ai: { text: string; model: string } | null
  aiError: string | null
  busy: 'facts' | 'ai' | null
  onRewrite: () => void
  onSelectRef?: (ref: ExplainRef) => void
}) {
  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs text-ink-300">
        <span className="font-semibold text-slate-100 capitalize">{data.kind}</span>
        {' · '}
        <span className={EVIDENCE_STYLE[data.evidence]}>{data.evidence} evidence</span>
        <span className="text-ink-500"> (a ranking aid built from checks, not a probability)</span>
      </p>

      {EXPLAIN_SECTIONS.map(({ key, title }) => {
        const items = data.sections[key]
        if (!items?.length) return null
        return (
          <section
            key={key}
            aria-label={title}
            className={key === 'conclusion' ? 'flex flex-col gap-1.5 border-l-2 border-accent pl-3' : 'flex flex-col gap-1.5'}
          >
            <h3 className="text-sm font-semibold text-slate-100">{title}</h3>
            <Sentences items={items} facts={data.facts} onSelectRef={onSelectRef} />
          </section>
        )
      })}

      {ai && <AiBox text={ai.text} model={ai.model} />}
      {data.ai_available && !ai && (
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className={BUTTON} onClick={onRewrite} disabled={busy !== null} aria-busy={busy === 'ai'}>
            {busy === 'ai' ? 'Writing…' : 'Rewrite in plainer language (AI)'}
          </button>
          <span className="text-[11px] text-ink-500">
            Optional. The AI only sees the measurements above and its answer is checked against them.
          </span>
        </div>
      )}
      {aiError && !ai && (
        <p role="status" className="text-xs text-ink-500">
          No AI summary: {aiError} The measurements above are unaffected.
        </p>
      )}
    </div>
  )
}

/**
 * "Explain this candidate": the measured evidence in plain language (what supports it, what makes
 * it uncertain, one check to do next, a careful conclusion). The text comes from the measurements
 * themselves; the AI rewrite is optional and only offered when the server has it.
 */
export function ExplainPanel({ fieldKey, candidateId, onSelectRef }: Props) {
  const regionId = useId()
  const { ready, processing, busy, error, ai, aiError, explain, rewrite } = useExplain(fieldKey, candidateId)
  const started = ready !== null || processing || busy !== null || error !== null

  return (
    <section className="text-[13px]" aria-labelledby={`${regionId}-title`}>
      <h2 id={`${regionId}-title`} className="section-title">
        Plain-language explanation
      </h2>

      {!started && (
        <div className="mt-2 flex flex-col items-start gap-2">
          <button type="button" className={PRIMARY} onClick={explain}>
            Explain this candidate
          </button>
          <p className="text-xs text-ink-500">
            Reads the measurements for you: what points to movement or a brightness change, and what could be wrong.
          </p>
        </div>
      )}

      <div aria-live="polite" className={started ? 'mt-2' : undefined}>
        {busy === 'facts' && (
          <p role="status" className="text-xs text-ink-300">
            Reading the measurements…
          </p>
        )}
        {processing && busy === null && (
          <div role="status" className="flex flex-wrap items-center gap-2 text-xs text-ink-300">
            <span>This field is still being analysed. Try again in a moment.</span>
            <button type="button" className={BUTTON} onClick={explain}>
              Try again
            </button>
          </div>
        )}
        {error && busy === null && (
          <div
            role="alert"
            className="flex flex-wrap items-center gap-2 rounded-md border border-red-900 bg-red-950/40 p-2 text-xs text-red-300"
          >
            <span>Could not explain this candidate: {error}</span>
            <button type="button" className={BUTTON} onClick={explain}>
              Retry
            </button>
          </div>
        )}
        {ready && (
          <Result
            data={ready}
            ai={ai}
            aiError={aiError}
            busy={busy}
            onRewrite={rewrite}
            onSelectRef={onSelectRef}
          />
        )}
      </div>
    </section>
  )
}
