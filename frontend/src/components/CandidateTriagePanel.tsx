import { useCallback, useEffect, useState } from 'react'
import type { Candidate } from '../lib/api'

interface Props {
  fieldKey: string
  candidates: Candidate[]
  activeCandidateId: string | null
  onSelectCandidate: (id: string) => void
}

type VoteKind = 'yes' | 'no' | 'unsure'

interface TriageState {
  votes: Record<string, VoteKind>
  timestamp: Record<string, string>
}

const storageKey = (field: string) => `spherex_triage_v2:${field}`

function loadTriage(fieldKey: string): TriageState {
  try {
    const raw = localStorage.getItem(storageKey(fieldKey))
    if (raw) {
      const parsed = JSON.parse(raw)
      if (parsed && typeof parsed.votes === 'object' && parsed.votes && typeof parsed.timestamp === 'object' && parsed.timestamp) {
        return { votes: Object.fromEntries(Object.entries(parsed.votes).filter(([,v]) => v==='yes' || v==='no' || v==='unsure')) as TriageState['votes'], timestamp: parsed.timestamp }
      }
    }
  } catch {
    // LocalStorage unavailable or corrupt
  }
  return { votes: {}, timestamp: {} }
}

function saveTriage(fieldKey: string, state: TriageState): void {
  try {
    localStorage.setItem(storageKey(fieldKey), JSON.stringify(state))
  } catch {
    // LocalStorage quota or access error
  }
}

export function CandidateTriagePanel({ fieldKey, candidates, activeCandidateId, onSelectCandidate }: Props) {
  const [triage, setTriage] = useState<TriageState>(() => loadTriage(fieldKey))
  const [showSummary, setShowSummary] = useState(false)

  // Current candidate
  const activeCandidate = candidates.find((c) => c.id === activeCandidateId) ?? candidates[0] ?? null
  const currentVote = activeCandidate ? triage.votes[activeCandidate.id] : undefined

  // Stats for current field
  const fieldVotes = candidates.map((c) => triage.votes[c.id]).filter(Boolean)
  const reviewedCount = fieldVotes.length
  const totalCount = candidates.length
  const progressPct = totalCount > 0 ? Math.round((reviewedCount / totalCount) * 100) : 0

  const yesCount = fieldVotes.filter((v) => v === 'yes').length
  const noCount = fieldVotes.filter((v) => v === 'no').length
  const unsureCount = fieldVotes.filter((v) => v === 'unsure').length

  const handleVote = useCallback(
    (vote: VoteKind) => {
      if (!activeCandidate) return
      const next: TriageState = {
        votes: { ...triage.votes, [activeCandidate.id]: vote },
        timestamp: { ...triage.timestamp, [activeCandidate.id]: new Date().toISOString() },
      }
      setTriage(next)
      saveTriage(fieldKey, next)

      // Advance to the next unvoted candidate if available
      const unvoted = candidates.find((c) => c.id !== activeCandidate.id && !next.votes[c.id])
      if (unvoted) {
        onSelectCandidate(unvoted.id)
      }
    },
    [activeCandidate, triage, candidates, onSelectCandidate, fieldKey],
  )

  // Keyboard hotkeys: Y = yes, N = no, U = unsure
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target instanceof Element ? e.target : null
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || t?.closest('input, textarea, select, [contenteditable], [role=dialog]') || document.querySelector('[aria-modal=true]')) return

      if (e.key === 'y' || e.key === 'Y') {
        handleVote('yes')
      } else if (e.key === 'n' || e.key === 'N') {
        handleVote('no')
      } else if (e.key === 'u' || e.key === 'U') {
        handleVote('unsure')
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [handleVote])

  const exportTriageCsv = () => {
    const rows = [
      ['Candidate_ID', 'Classification', 'Kind', 'RA_deg', 'Dec_deg', 'Max_Z', 'PM_Total_masyr', 'Timestamp'],
    ]
    for (const c of candidates) {
      const v = triage.votes[c.id] ?? 'unreviewed'
      const ts = triage.timestamp[c.id] ?? ''
      rows.push([
        c.id,
        v,
        c.kind,
        c.ra.toFixed(6),
        c.dec.toFixed(6),
        c.max_z.toFixed(2),
        c.pm_total_masyr ? c.pm_total_masyr.toFixed(1) : '–',
        ts,
      ])
    }
    const csvContent = rows.map((r) => r.join(',')).join('\n')
    const blob = new Blob([csvContent], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `gamas-triage-session-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  if (candidates.length === 0) return null

  return (
    <div className="flex flex-col gap-3 rounded-[var(--radius-panel)] border border-[var(--rule)] bg-[var(--plate)] p-4 text-xs shadow-md">
      {/* 7.2 Heading: plain question */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-full bg-[var(--hi)]" />
          <h3 className="font-display font-bold text-base text-white tracking-tight">
            Is this a real object?
          </h3>
        </div>

        <button
          type="button"
          onClick={() => setShowSummary(!showSummary)}
          className="text-xs text-[var(--mute)] hover:text-white transition-colors"
        >
          {showSummary ? 'Hide stats' : 'Session stats'}
        </button>
      </div>

      {/* Active candidate summary */}
      {activeCandidate && (
        <div className="flex items-center justify-between gap-2 text-xs text-[var(--mute)]">
          <span className="flex items-center gap-1.5">
            <span>Inspecting candidate:</span>
            <strong className="text-white font-display font-bold">{activeCandidate.id}</strong>
            <span className="capitalize">({activeCandidate.kind})</span>
          </span>
          <span className="tabular text-[11px]">
            Peak SNR: <strong className="text-[var(--pen)]">{activeCandidate.max_z.toFixed(1)}σ</strong>
          </span>
        </div>
      )}

      {/* 7.2 Decision buttons: 3 pills with --hi active */}
      <div className="flex items-center gap-2 pt-1">
        <button
          type="button"
          onClick={() => handleVote('yes')}
          className={`flex-1 py-1.5 px-3 rounded-[var(--radius-pill)] border font-semibold text-xs transition-all ${
            currentVote === 'yes'
              ? 'bg-[var(--hi)] text-[#060b17] border-[var(--hi)] shadow-sm'
              : 'border-[var(--rule)] bg-[var(--bg)]/60 text-[var(--ink)] hover:border-[var(--pen)]'
          }`}
          title="Mark candidate as a genuine celestial object (Press Y)"
        >
          Real object
        </button>

        <button
          type="button"
          onClick={() => handleVote('no')}
          className={`flex-1 py-1.5 px-3 rounded-[var(--radius-pill)] border font-semibold text-xs transition-all ${
            currentVote === 'no'
              ? 'bg-[var(--hi)] text-[#060b17] border-[var(--hi)] shadow-sm'
              : 'border-[var(--rule)] bg-[var(--bg)]/60 text-[var(--ink)] hover:border-[var(--pen)]'
          }`}
          title="Mark candidate as an optical artifact or noise (Press N)"
        >
          Artefact
        </button>

        <button
          type="button"
          onClick={() => handleVote('unsure')}
          className={`py-1.5 px-3 rounded-[var(--radius-pill)] border font-semibold text-xs transition-all ${
            currentVote === 'unsure'
              ? 'bg-[var(--hi)] text-[#060b17] border-[var(--hi)] shadow-sm'
              : 'border-[var(--rule)] bg-[var(--bg)]/60 text-[var(--mute)] hover:border-[var(--pen)]'
          }`}
          title="Mark as uncertain (Press U)"
        >
          Not sure
        </button>
      </div>

      {/* 7.2 Progress bar: 4px high, rounded, band colours in gradient */}
      <div className="flex flex-col gap-1 pt-1">
        <div className="h-1 w-full overflow-hidden rounded-[var(--radius-pill)] bg-[var(--bg)]">
          <div
            className="h-full rounded-[var(--radius-pill)] transition-all duration-300"
            style={{
              width: `${Math.max(5, progressPct)}%`,
              backgroundImage: 'linear-gradient(to right, var(--b1), var(--b3), var(--b5), var(--b6))',
            }}
          />
        </div>

        {/* 7.2 Triage count text */}
        <div className="flex items-center justify-between text-[11px] tabular text-[var(--mute)]">
          <span>{reviewedCount} citizen votes recorded</span>
          <span>{progressPct}% field reviewed</span>
        </div>
      </div>

      {/* Session summary expandable */}
      {showSummary && (
        <div className="flex flex-col gap-2 rounded-[var(--radius-panel)] border border-[var(--rule)] bg-[var(--bg)]/60 p-3 text-xs mt-1">
          <div className="font-display font-bold text-white">Consensus breakdown</div>
          <div className="flex items-center gap-4 text-[var(--mute)] tabular">
            <span>Real: <strong className="text-[var(--ok)]">{yesCount}</strong></span>
            <span>Artefact: <strong className="text-[var(--warm)]">{noCount}</strong></span>
            <span>Unsure: <strong className="text-[var(--ink)]">{unsureCount}</strong></span>
            <span>Unreviewed: <strong>{totalCount - reviewedCount}</strong></span>
          </div>

          <div className="flex items-center justify-between border-t border-[var(--rule)] pt-2 mt-1">
            <span className="text-[11px] text-[var(--mute)]">Stored in browser local storage</span>
            <button
              type="button"
              onClick={exportTriageCsv}
              className="btn btn-quiet text-xs py-0.5 px-2 hover:border-[var(--pen)] hover:text-white"
            >
              Export CSV
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
