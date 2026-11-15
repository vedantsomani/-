import { useState } from "react";
import type {
  Analysis,
  Candidate,
  ChangeEvent,
  ChangeKind,
  EvidenceCheck,
  EvidenceLevel,
} from "../lib/api";
import { detectorTextColor } from "../lib/constants";
import { IconCheck, IconCross } from "./icons";
import type { ProperMotion } from "../lib/coords";
import type { OverlayData } from "../lib/overlay";
import type { ViewState } from "../lib/viewState";

export const KindBadge = ({ kind }: { kind: string }) => (
  <span className={`badge ${kind.replace(/\s+/g, "-")}`}>{kind}</span>
);

const fmtPm = (ra?: number, de?: number) =>
  ra === undefined || de === undefined
    ? ""
    : `(${(ra / 1000).toFixed(2)}, ${(de / 1000).toFixed(2)})″/yr`;

/** Evidence level with the checks behind it. Deliberately not a percentage: the level
 *  comes from explicit checks and has not been calibrated against labelled data. */
const EvidenceTag = ({ level }: { level: EvidenceLevel }) => (
  <span className={`badge ${level}`} title="Evidence level from explicit checks; not a probability">
    {level} evidence
  </span>
);

const Checks = ({ checks }: { checks: EvidenceCheck[] }) => (
  <ul className="flex flex-col gap-1 text-[13px]">
    {checks.map((c, i) => (
      <li key={i} className={`flex gap-2 ${c.ok ? "" : "text-ink-400"}`}>
        {c.ok ? (
          <IconCheck className="mt-0.5 shrink-0" style={{ color: "var(--success)" }} />
        ) : (
          <IconCross className="mt-0.5 shrink-0" style={{ color: "var(--variable)" }} />
        )}
        <span>
          {c.label}
          {c.note && <span className="text-ink-500"> ({c.note})</span>}
        </span>
      </li>
    ))}
  </ul>
);

const lamRange = (e: ChangeEvent) =>
  e.lam_lo_um !== undefined && e.lam_hi_um !== undefined
    ? `${e.lam_lo_um.toFixed(2)}–${e.lam_hi_um.toFixed(2)} µm`
    : "";

function evidence(e: ChangeEvent): string {
  if (e.kind === "mover")
    return `shift ${e.shift_px?.toFixed(2)} px (${e.shift_sigma}σ)`;
  if (e.kind === "variable")
    return `×${e.flux_ratio?.toFixed(2)} (${e.dflux_sigma}σ)`;
  if (e.kind === "static")
    return `×${e.flux_ratio?.toFixed(2)}, shift ${e.shift_px?.toFixed(2)} px`;
  if (e.kind === "appeared" || e.kind === "vanished")
    return `Δ ${e.dflux_sigma}σ`;
  return e.reason ?? `S/N ${e.snr_a ?? "–"} → ${e.snr_b ?? "–"}`;
}

interface Props {
  analysis: Analysis;
  seasonLabel: (s: number) => string;
  catalogPm?: ProperMotion;
  view: ViewState;
  update: (patch: Partial<ViewState>) => void;
  /** Gaia cross-match of the candidates, when the overlay has loaded. */
  overlay?: OverlayData | null;
}

/** Catalogue label (and the new-object badge) for one candidate, from the Gaia cross-match. */
function GaiaChips({ overlay, id }: { overlay: OverlayData; id: string }) {
  const g = overlay.candidates[id];
  const wise = overlay.wise?.candidates[id];
  if (!g && !wise?.designation) return null;
  const caveat =
    overlay.notes.find((n) => n.startsWith("Absence from Gaia")) ??
    overlay.notes[0] ??
    "";
  const cw = g?.catwise_match;
  return (
    <div className="flex flex-wrap items-center gap-1">
      {overlay.wise?.candidates[id]?.designation && (
        <span className="chip">
          In AllWISE: WISE {overlay.wise.candidates[id].designation}
        </span>
      )}
      {g &&
        (g.candidate_new_object ? (
          <span
            className="chip" style={{ color: "var(--vanished)", borderColor: "currentColor" }}
            title={`Strong or moderate mover evidence and no Gaia, CatWISE2020, or AllWISE source nearby. ${caveat}`}
          >
            Candidate new object (no catalogue match)
          </span>
        ) : (
          <span
            className="chip"
            title={
              g.label_note ??
              (cw
                ? `CatWISE2020 ${cw.source_name}, ${cw.separation_arcsec.toFixed(1)} arcsec away`
                : "Gaia DR3 and CatWISE2020 cross-match within 6 arcsec at this candidate’s epoch")
            }
          >
            {g.label}
          </span>
        ))}
    </div>
  );
}

export function DetectionsPanel({
  analysis,
  seasonLabel,
  catalogPm,
  view,
  update,
  overlay,
}: Props) {
  const [showAll, setShowAll] = useState(false);
  const t = analysis.target;
  const cands = analysis.candidates.filter(
    (c) => showAll || c.evidence !== "weak",
  );
  const hidden = analysis.candidates.length - cands.length;
  const fast = analysis.fast_movers.filter(
    (f) => f.kind !== "stationary_transient" || showAll,
  );

  const focus = (c: Candidate) => {
    const best = [...c.events].sort(
      (a, b) =>
        kindRank(a.kind) - kindRank(b.kind) || (b.max_z ?? 0) - (a.max_z ?? 0),
    )[0];
    update({
      mode: "diff",
      det: best.detector,
      pair: [best.season_a, best.season_b],
      diffCombined: true,
      cand: c.id,
      marks: true,
    });
  };

  const speed =
    t.pmra_masyr !== undefined && t.pmdec_masyr !== undefined
      ? Math.hypot(t.pmra_masyr, t.pmdec_masyr) / 1000
      : null;

  return (
    <section className="flex flex-col gap-5">
      <div className="verdict">
        <p className="field-label">Target verdict</p>
        <div className="verdict-head">
          <span className="verdict-kind">{t.summary}</span>
          <EvidenceTag level={t.evidence} />
        </div>
        {speed !== null && (
          <dl className="kv">
            <dt>Measured motion</dt>
            <dd>
              {speed.toFixed(2)}″/yr <span className="text-ink-500">μ = {fmtPm(t.pmra_masyr, t.pmdec_masyr)}</span>
            </dd>
            {catalogPm && (
              <>
                <dt>Catalogue</dt>
                <dd>
                  {(Math.hypot(catalogPm.pmra, catalogPm.pmdec) / 1000).toFixed(2)}″/yr{" "}
                  <span className="text-ink-500">μ = {fmtPm(catalogPm.pmra, catalogPm.pmdec)}</span>
                </dd>
              </>
            )}
            {t.motion_check && t.motion_check.n_movers >= 2 && (
              <>
                <dt>Coherence</dt>
                <dd>
                  {t.motion_check.coherence?.toFixed(2)} over {t.motion_check.n_movers} shifts, speed scatter{" "}
                  {Math.round((t.motion_check.speed_scatter ?? 0) * 100)}%
                  {" · "}
                  {t.motion_check.coherent ? (
                    <span style={{ color: "var(--success)" }}>consistent proper motion</span>
                  ) : (
                    <span style={{ color: "var(--variable)" }}>inconsistent, so not motion (blend, saturation or variability)</span>
                  )}
                </dd>
              </>
            )}
          </dl>
        )}
        {speed === null && t.motion_check && t.motion_check.n_movers >= 2 && (
          <p className="help">
            Motion check: {t.motion_check.n_movers} centroid shifts, coherence {t.motion_check.coherence?.toFixed(2)},{" "}
            {t.motion_check.coherent ? "consistent proper motion" : "inconsistent, so not motion (blend, saturation or variability)"}
          </p>
        )}
        <Checks checks={t.checks} />
        <details className="text-xs">
          <summary className="cursor-pointer text-ink-300 hover:text-slate-100">
            Measurements per detector and season pair
          </summary>
          <p className="help mt-1">Each pair is compared at matched wavelengths.</p>
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-left text-[11px] text-ink-500">
                <tr>
                  <th className="py-1 pr-2 font-medium">Det.</th>
                  <th className="pr-2 font-medium">Seasons</th>
                  <th className="pr-2 font-medium">Kind</th>
                  <th className="pr-2 font-medium">Measure</th>
                  <th className="text-right font-medium">λ (µm)</th>
                </tr>
              </thead>
              <tbody>
                {t.rows.map((r, i) => (
                  <tr key={i} className="border-t border-ink-800">
                    <td className="py-1 pr-2" style={{ color: detectorTextColor(r.detector) }}>
                      D{r.detector}
                    </td>
                    <td className="pr-2 whitespace-nowrap text-ink-400">
                      {seasonLabel(r.season_a).slice(2)}→{seasonLabel(r.season_b).slice(2)}
                    </td>
                    <td className="pr-2">{r.kind}</td>
                    <td
                      className="pr-2 text-ink-300"
                      title={[
                        lamRange(r) && `compared over ${lamRange(r)} (${r.bins?.length} bins)`,
                        r.bins_missing?.length ? `not comparable: ${r.bins_missing.join(", ")}` : "",
                        r.method,
                        r.measure?.aperture,
                        r.reason,
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    >
                      {evidence(r)}
                      {r.method && <span className="text-ink-500"> · fit</span>}
                    </td>
                    <td className="text-right whitespace-nowrap text-ink-500">{lamRange(r).replace(" µm", "")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      </div>

      <div className="inspector-block flex flex-col gap-2">
        <div className="flex items-baseline justify-between gap-2">
          <h2 className="text-[13px] font-semibold">Changes in the field</h2>
          <label className="flex items-center gap-1.5 text-xs text-ink-400">
            <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />
            Show weak{hidden > 0 && !showAll ? ` (${hidden})` : ""}
          </label>
        </div>
        {cands.length === 0 && <p className="help">No significant movers or variables between seasons.</p>}
        <ul className="flex flex-col gap-1.5">
          {cands.map((c) => (
            <li key={c.id}>
              <button
                onClick={() => focus(c)}
                aria-pressed={view.cand === c.id}
                title="Show this change in the difference image"
                className={`flex w-full flex-col gap-1.5 rounded-md border px-3 py-2 text-left text-xs transition-colors ${
                  view.cand === c.id ? "border-accent/60 bg-ink-800" : "border-ink-800 hover:border-ink-600 hover:bg-ink-900"
                }`}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <KindBadge kind={c.kind} />
                  {c.is_target && <span className="chip">Target</span>}
                  <span className="mono text-ink-300">
                    {c.ra.toFixed(5)} {c.dec >= 0 ? "+" : "−"}
                    {Math.abs(c.dec).toFixed(5)}
                  </span>
                  <span className="ml-auto">
                    <EvidenceTag level={c.evidence} />
                  </span>
                </div>
                <div className="tabular text-ink-400">
                  {c.detectors.map((d) => (
                    <span key={d} style={{ color: detectorTextColor(d) }}>
                      D{d}{" "}
                    </span>
                  ))}
                  · {c.n_independent} independent comparison
                  {c.n_independent === 1 ? "" : "s"} · peak {c.max_z}σ
                  {c.pm_total_masyr !== undefined && ` · μ = ${fmtPm(c.pmra_masyr, c.pmdec_masyr)}`}
                  {c.flux_ratio_min !== undefined &&
                    ` · flux ×${c.flux_ratio_min.toFixed(2)}–${c.flux_ratio_max?.toFixed(2)}`}
                </div>
                {overlay && <GaiaChips overlay={overlay} id={c.id} />}
                {c.reason && <div className="text-ink-500">{c.reason}</div>}
                {view.cand === c.id && <Checks checks={c.checks} />}
              </button>
            </li>
          ))}
        </ul>
      </div>

      {fast.length > 0 && (
        <div className="inspector-block flex flex-col gap-2">
          <h2 className="text-[13px] font-semibold">Fast movers within a visit</h2>
          <ul className="flex flex-col gap-1.5 text-xs">
            {fast.map((f, i) => (
              <li key={i}>
                <button
                  className="w-full rounded-md border border-ink-800 px-3 py-2 text-left tabular hover:border-ink-600 hover:bg-ink-900"
                  onClick={() =>
                    update({
                      mode: "frames",
                      dets: [...new Set(f.points.map((p) => p.detector))],
                      marks: true,
                    })
                  }
                >
                  <span className="font-medium">{f.kind.replace("_", " ")}</span>
                  <span className="text-ink-400">
                    {" "}
                    · {f.date.slice(0, 16).replace("T", " ")} ·{" "}
                    {f.rate_arcsec_per_min !== null
                      ? `${f.rate_arcsec_per_min.toFixed(2)}″/min, PA ${f.pa_deg}°`
                      : "single epoch"}{" "}
                    · {f.points.length} detections
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function kindRank(k: ChangeKind): number {
  return {
    mover: 0,
    variable: 1,
    appeared: 2,
    vanished: 2,
    uncertain: 3,
    static: 4,
  }[k];
}
