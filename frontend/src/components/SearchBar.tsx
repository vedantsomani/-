import { useId, useState, type FormEvent } from 'react'
import { resolveName } from '../lib/api'
import { FOV_OPTIONS } from '../lib/constants'
import { J2000_MJD, SPHEREX_REF_MJD, parsePosition, propagate } from '../lib/coords'
import type { Target } from '../lib/viewState'
import { IconSearch } from './icons'

/** Above this total proper motion (mas/yr) we re-centre on the SPHEREx-epoch position. */
const PM_THRESHOLD = 200

export const SEARCH_INPUT_ID = 'q'

interface Props {
  fov: number
  onFovChange: (fov: number) => void
  onTarget: (t: Target) => void
  busy?: boolean
  /** `hero`: large, labelled, on the entry screen. `compact`: one row in the header. */
  variant?: 'hero' | 'compact'
}

const FOV_HELP =
  'Field of view: how wide an area of sky to show, in arcminutes (the full Moon is about 30′ across). Small fields load faster.'

const looksOffline = (m: string) => /failed to fetch|networkerror|load failed|network request failed/i.test(m)

export function SearchBar({ fov, onFovChange, onTarget, busy, variant = 'compact' }: Props) {
  const [text, setText] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [resolving, setResolving] = useState(false)
  const errorId = useId()
  const hero = variant === 'hero'

  async function go(query: string) {
    setError(null)
    const q = query.trim()
    if (!q) {
      setError('Type an object name (for example Barnard’s Star) or coordinates (for example 133.795 -7.245).')
      return
    }
    const pos = parsePosition(q)
    if (pos) {
      onTarget({ ...pos, label: q })
      return
    }
    setResolving(true)
    try {
      const r = await resolveName(q)
      const label = r.name.replace(/^NAME /, '')
      const pmra = r.pm_ra_masyr ?? 0
      const pmdec = r.pm_dec_masyr ?? 0
      if (Math.hypot(pmra, pmdec) > PM_THRESHOLD) {
        const at = propagate(r.ra, r.dec, { pmra, pmdec, epochMjd: J2000_MJD }, SPHEREX_REF_MJD)
        onTarget({ ...at, label, pm: { pmra, pmdec, epochMjd: SPHEREX_REF_MJD } })
      } else {
        onTarget({ ra: r.ra, dec: r.dec, label })
      }
    } catch (e) {
      const m = (e as Error).message
      setError(
        looksOffline(m)
          ? 'Can’t reach the name lookup right now. Coordinates such as 133.795 -7.245 still work, or pick an example.'
          : `We couldn’t find “${q}”. Check the spelling, or type coordinates such as 133.795 -7.245.`,
      )
    } finally {
      setResolving(false)
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    go(text)
  }

  const fovSelect = (
    <select
      id={`${SEARCH_INPUT_ID}-fov`}
      value={fov}
      onChange={(e) => onFovChange(Number(e.target.value))}
      className="search-fov"
      title={FOV_HELP}
      aria-label="Field of view in arcminutes"
    >
      {FOV_OPTIONS.map((f) => (
        <option key={f} value={f}>
          {f}′
        </option>
      ))}
    </select>
  )

  return (
    <div className={`search ${hero ? 'search-hero' : 'search-compact'}`}>
      <form onSubmit={submit} role="search" className="search-form">
        <label className="sr-only" htmlFor={SEARCH_INPUT_ID}>
          Object name or coordinates
        </label>
        <div className="search-box">
          <IconSearch className="search-icon" />
          <input
            id={SEARCH_INPUT_ID}
            value={text}
            onChange={(e) => setText(e.target.value)}
            type="search"
            enterKeyHint="go"
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? errorId : hero ? `${errorId}-hint` : undefined}
            placeholder={hero ? 'Object name or RA Dec, e.g. Barnard’s Star' : 'Name or RA Dec'}
          />
          {fovSelect}
          <button type="submit" disabled={resolving || busy} className={`btn btn-primary ${hero ? '' : 'btn-sm'}`}>
            {resolving ? 'Looking up…' : hero ? 'Search' : 'Go'}
          </button>
        </div>
        {hero && (
          <p id={`${errorId}-hint`} className="help mt-2">
            A name, or coordinates such as <span className="mono">133.795 -7.245</span> or{' '}
            <span className="mono">08h55m10.8s -07d14m42s</span>. Field of view in arcminutes.
          </p>
        )}
      </form>
      {error && (
        <p id={errorId} role="alert" className={`search-error ${hero ? '' : 'is-floating'}`}>
          {error}
        </p>
      )}
    </div>
  )
}
