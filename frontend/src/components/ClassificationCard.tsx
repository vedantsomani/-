import type { ClassificationConfidence, FingerprintClassification, SpectralIndex } from '../lib/api'
import { isNum } from '../lib/chart'

const CHIP: Record<ClassificationConfidence, string> = {
  high: 'border-emerald-400/40 bg-emerald-400/15 text-emerald-200',
  medium: 'border-amber-400/40 bg-amber-400/15 text-amber-200',
  low: 'border-ink-600 bg-ink-800 text-ink-300',
}

const range = (r: [number, number]) => `${r[0]}–${r[1]}`

function IndexRow({ name, ix }: { name: string; ix: SpectralIndex }) {
  const measured = isNum(ix.value) && isNum(ix.err)
  const sigLabel = ix.kind === 'absorption' ? 'depth' : 'S/N'
  const title = `${ix.feature}: band ${range(ix.band_um)} µm vs ${ix.continuum_um.map(range).join(' & ')} µm${ix.note ? ` (${ix.note})` : ''}`
  return (
    <tr className="border-t border-ink-800" title={title}>
      <th scope="row" className="px-2 py-1 text-left font-normal">
        <span className="font-mono text-slate-200">{name}</span>
        <span className="ml-1.5 text-ink-500">{ix.feature}</span>
      </th>
      <td className="px-2 py-1 font-mono">{measured ? `${ix.value!.toFixed(2)} ± ${ix.err!.toFixed(2)}` : '–'}</td>
      <td className="px-2 py-1 font-mono text-ink-300">
        {measured && isNum(ix.sigma) ? `${ix.sigma.toFixed(1)}σ ${sigLabel}` : (ix.note ?? '–')}
      </td>
    </tr>
  )
}

export function ClassificationCard({ classification: c }: { classification: FingerprintClassification }) {
  const entries = Object.entries(c.indices ?? {})
  return (
    <section className="flex flex-col gap-3 rounded-lg border border-line bg-ink-950 p-3" aria-label="What is it">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-slate-200">What is it?</h3>
        <span className="text-[15px] font-semibold text-slate-100">{c.label}</span>
        <span
          className={`cursor-help rounded border px-1.5 py-px text-[11px] font-medium ${CHIP[c.confidence] ?? CHIP.low}`}
          title={c.confidence_note || 'rule-based, not a probability'}
        >
          {c.confidence} confidence
          <span className="sr-only"> ({c.confidence_note || 'rule-based, not a probability'})</span>
        </span>
      </div>

      {c.reasons.length > 0 && (
        <ul className="list-disc pl-4 text-xs text-slate-300">
          {c.reasons.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      )}

      {entries.length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer select-none text-ink-300 hover:text-slate-100">Spectral indices</summary>
          <div className="mt-2 overflow-x-auto rounded border border-ink-800">
            <table className="w-full min-w-[26rem] text-left text-xs">
              <caption className="sr-only">
                Flux ratios: absorption indices are band over interpolated continuum (1 = no absorption); colours are
                ratios of window means.
              </caption>
              <thead className="bg-ink-900 text-ink-300">
                <tr>
                  <th scope="col" className="px-2 py-1.5 font-semibold">Index</th>
                  <th scope="col" className="px-2 py-1.5 font-semibold">Ratio</th>
                  <th scope="col" className="px-2 py-1.5 font-semibold">Significance</th>
                </tr>
              </thead>
              <tbody>
                {entries.map(([name, ix]) => (
                  <IndexRow key={name} name={name} ix={ix} />
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}

      {c.caveats.length > 0 && (
        <ul className="list-disc pl-4 text-xs text-ink-500">
          {c.caveats.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      )}
    </section>
  )
}
