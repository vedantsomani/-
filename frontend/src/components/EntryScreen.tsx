import { useEffect, useState } from "react";
import { useReducedMotion } from "../hooks/useReducedMotion";
import { apiUrl } from "../lib/api";
import { EXAMPLE_TARGETS } from "../lib/constants";
import { pmOffsetArcsec } from "../lib/coords";
import { isPlainClick } from "../lib/nav";
import type { Target } from "../lib/viewState";
import { SearchBar } from "./SearchBar";

interface Props {
  fov: number;
  onFovChange: (f: number) => void;
  onTarget: (t: Target) => void;
  onOpenExample: (s: string) => void;
  onStartTour: () => void;
  onOpenBatch: () => void;
}

/** The hero demo: WISE 0855−0714 in D6 (4.4–5.0 µm), one season stack per observing season.
 *  Images come live from the API (cached field); bundled copies of the same PNGs are the fallback.
 *  Numbers are the pipeline's own output for this field (docs/validation.md). */
const DEMO = {
  example: EXAMPLE_TARGETS[0],
  fieldKey: "63adfc52c96d65b9",
  gridN: 49,
  pixscale: 6.15,
  pm: { pmra: -8123.7, pmdec: 673.2, epochMjd: 61000 },
  measuredMasYr: Math.hypot(-7661, 1192),
  className: "Y dwarf-like",
  seasons: [
    { season: 0, label: "May 2025", mjd: 60800.746 },
    { season: 1, label: "Nov 2025", mjd: 60995.818 },
    { season: 2, label: "Apr 2026", mjd: 61157.93 },
  ],
};
const RENDER = "stretch=asinh&scale=detector&sig_lo=-2&sig_hi=30";
const liveUrl = (s: number) => apiUrl(`/api/fields/${DEMO.fieldKey}/stacks/s${s}_d6.png?${RENDER}`);
const bundledUrl = (s: number) => `/demo/wise0855-s${s}.png`;

function HeroDemo({ onOpen }: { onOpen: () => void }) {
  const reduced = useReducedMotion();
  const [i, setI] = useState(0);
  const [playing, setPlaying] = useState(!reduced);
  const [fallback, setFallback] = useState<Record<number, boolean>>({});
  const [ready, setReady] = useState<Record<number, boolean>>({});

  useEffect(() => {
    if (!playing) return;
    const t = window.setInterval(() => setI((x) => (x + 1) % DEMO.seasons.length), 750);
    return () => window.clearInterval(t);
  }, [playing]);

  const c = (DEMO.gridN - 1) / 2 + 0.5;
  return (
    <figure className="demo" aria-label="Live example: WISE 0855−0714 blinking between three SPHEREx observing seasons">
      <a
        href={DEMO.example.search}
        className="demo-frame"
        onClick={(e) => {
          if (!isPlainClick(e)) return;
          e.preventDefault();
          onOpen();
        }}
        aria-label="Open WISE 0855−0714 in the viewer"
      >
        {DEMO.seasons.map((s, k) => (
          <img
            key={s.season}
            src={fallback[s.season] ? bundledUrl(s.season) : liveUrl(s.season)}
            alt=""
            width={DEMO.gridN}
            height={DEMO.gridN}
            decoding="async"
            onLoad={() => setReady((r) => ({ ...r, [s.season]: true }))}
            onError={() => setFallback((f) => (f[s.season] ? f : { ...f, [s.season]: true }))}
            style={{ opacity: k === i && ready[s.season] ? 1 : 0 }}
          />
        ))}
        <svg viewBox={`0 0 ${DEMO.gridN} ${DEMO.gridN}`} aria-hidden="true">
          {(() => {
            const o = pmOffsetArcsec(DEMO.pm, DEMO.seasons[i].mjd);
            return <circle cx={c - o.east / DEMO.pixscale} cy={c - o.north / DEMO.pixscale} r={2.6} fill="none" stroke="#5c9cff" strokeWidth={0.22} />;
          })()}
        </svg>
      </a>
      <figcaption className="demo-caption">
        <span className="flex flex-col gap-0.5">
          <strong>WISE 0855−0714</strong>
          <span>D6 · 4.4–5.0 µm · season stacks · 5′ field</span>
        </span>
        <span className="demo-seasons" role="group" aria-label="Season shown">
          {DEMO.seasons.map((s, k) => (
            <button
              key={s.season}
              type="button"
              className={`btn btn-ghost btn-sm px-2 ${k === i ? "text-[var(--t1)]" : ""}`}
              aria-pressed={k === i}
              onClick={() => {
                setPlaying(false);
                setI(k);
              }}
            >
              {s.label}
            </button>
          ))}
        </span>
      </figcaption>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className="verdict-chip" title="Pipeline verdict for this field: motion measured in season differences, spectrum classified from the deblended photometry">
          <i aria-hidden="true" />
          <span className="tabular">{`Mover · ${(DEMO.measuredMasYr / 1000).toFixed(2)}″/yr · ${DEMO.className}`}</span>
        </span>
        <span className="help">The ring is where the catalogue motion predicts the source on each date.</span>
      </div>
    </figure>
  );
}

const MEASURES: [string, string][] = [
  ["Motion", "Proper motion from centroid shifts, checked across detectors and seasons"],
  ["Brightness change", "Flux ratio between seasons at matched wavelengths"],
  ["Spectrum", "0.75–5 µm, PSF photometry with neighbours fitted out"],
  ["Classification", "Rule-based label from spectral indices, never a probability"],
  ["Hold-out test", "Predict an unseen season’s position, then reveal it"],
];

export function EntryScreen(p: Props) {
  const ordered = EXAMPLE_TARGETS;
  const open = (search: string) => (e: React.MouseEvent) => {
    if (!isPlainClick(e)) return;
    e.preventDefault();
    p.onOpenExample(search);
  };
  return (
    <div className="landing">
      <section className="hero" aria-labelledby="hero-title">
        <div>
          <h1 id="hero-title">See what moved in the infrared sky.</h1>
          <p className="hero-lede">
            Enter any position. GAMAS blinks NASA SPHEREx images taken months apart, finds sources that moved or changed
            brightness, measures their 0.75–5 µm spectrum, and tests each motion by predicting a season it has not seen.
          </p>
          <div className="hero-search">
            <SearchBar variant="hero" fov={p.fov} onFovChange={p.onFovChange} onTarget={p.onTarget} />
          </div>
          <p className="hero-try">
            <span>Try</span>
            {ordered.slice(0, 4).map((ex) => (
              <a key={ex.id} href={ex.search} onClick={open(ex.search)}>
                {ex.name}
              </a>
            ))}
            <button type="button" className="text-link text-xs" onClick={p.onStartTour}>
              Take the 1-minute tour
            </button>
          </p>
        </div>
        <HeroDemo onOpen={() => p.onOpenExample(DEMO.example.search)} />
      </section>

      <section className="l-section" aria-labelledby="examples-title">
        <div className="l-head">
          <h2 id="examples-title">Five fields to start with</h2>
          <p>Cached, so they open instantly.</p>
        </div>
        <ul className="examples" role="list">
          {ordered.map((ex) => (
            <li key={ex.id} className="contents">
              <a href={ex.search} className="example" onClick={open(ex.search)}>
                <img src={`/examples/${ex.id}.png`} alt={`SPHEREx D${ex.bestDetector} season stack of ${ex.name}`} width={49} height={49} loading="lazy" />
                <div className="flex flex-col gap-1">
                  <h3>{ex.name}</h3>
                  <p>{ex.tag}</p>
                  <span className="num">
                    D{ex.bestDetector} · {ex.motionPerYear}
                  </span>
                </div>
              </a>
            </li>
          ))}
        </ul>
      </section>

      <section className="l-section" aria-labelledby="how-title">
        <div className="l-head">
          <h2 id="how-title">How it works</h2>
        </div>
        <ol className="steps m-0 p-0">
          <li>
            <h3>Blink</h3>
            <p>Season stacks of one detector, aligned on the same grid, flip in place so anything that moved jumps.</p>
          </li>
          <li>
            <h3>Detect</h3>
            <p>Wavelength-matched differences flag changes; a mover must agree in direction and speed across pairs.</p>
          </li>
          <li>
            <h3>Measure and test</h3>
            <p>A deblended spectrum classifies the source, and a hold-out season checks the motion prediction.</p>
          </li>
        </ol>
      </section>

      <section className="l-section" aria-labelledby="measure-title">
        <div className="l-head">
          <h2 id="measure-title">What you can measure</h2>
        </div>
        <dl className="measures m-0">
          {MEASURES.map(([k, v]) => (
            <div key={k}>
              <dt>{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
        <div className="l-links">
          <button type="button" className="btn" onClick={p.onOpenBatch}>
            Triage a list of targets
          </button>
          <button type="button" className="btn btn-ghost" onClick={p.onStartTour}>
            Guided tour
          </button>
        </div>
      </section>

      <footer className="credits">
        <p>
          GAMAS · The changing sky. An independent NASA Space Apps 2026 project; not affiliated with or endorsed by NASA.
          <br />
          Detections are candidates and need follow-up.
        </p>
        <p>
          Data: NASA SPHEREx via{" "}
          <a href="https://irsa.ipac.caltech.edu/" target="_blank" rel="noreferrer">
            NASA/IPAC IRSA
          </a>
          . Catalogues: Gaia DR3 (ESA), CatWISE2020, AllWISE. Sky context:{" "}
          <a href="https://aladin.cds.unistra.fr/" target="_blank" rel="noreferrer">
            CDS Aladin Lite
          </a>
          .
        </p>
      </footer>
    </div>
  );
}
