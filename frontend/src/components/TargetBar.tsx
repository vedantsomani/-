import { useEffect, useState } from "react";
import { formatDec, formatRa } from "../lib/coords";
import { copyLink, reportUrl } from "../lib/share";
import type { Target } from "../lib/viewState";
import type { Phase } from "../lib/fieldPhase";
import { IconImage, IconLink, IconReport } from "./icons";

interface Props {
  target: Target;
  fov: number;
  phase: Phase;
  onExportDiscovery?: () => void;
}

/** One thin strip above the image: what you are looking at, pipeline status, and the share actions. */
export function TargetBar({ target, fov, phase, onExportDiscovery }: Props) {
  const [copy, setCopy] = useState<"idle" | "ok" | "fail">("idle");
  useEffect(() => {
    if (copy === "idle") return;
    const t = window.setTimeout(() => setCopy("idle"), 2200);
    return () => window.clearTimeout(t);
  }, [copy]);
  const speed = target.pm ? Math.hypot(target.pm.pmra, target.pm.pmdec) / 1000 : null;

  return (
    <div className="target-strip">
      <div className="target-id">
        <h1 className="target-name" title={target.label || "Custom position"}>
          {target.label || "Custom position"}
        </h1>
        <span className="target-meta">
          <span className="mono" title={`${formatRa(target.ra)} ${formatDec(target.dec)}`}>
            {target.ra.toFixed(5)}° {target.dec >= 0 ? "+" : "−"}
            {Math.abs(target.dec).toFixed(5)}°
          </span>
          <span className="tabular">{fov}′ field</span>
          {speed !== null && <span className="tabular" title="Catalogue proper motion">μ {speed.toFixed(2)}″/yr</span>}
        </span>
        <span className="status-text" aria-hidden="true">
          <span className={`status-dot ${phase.busy ? "is-busy" : phase.label && !phase.label.startsWith("Loading") ? "is-ok" : ""}`} />
          {phase.label}
        </span>
      </div>
      <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {phase.spoken}
      </p>
      <div className="target-actions">
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={async () => setCopy((await copyLink()) ? "ok" : "fail")}
          title="Copy a link to exactly this view"
          aria-label="Copy link to this view"
        >
          <IconLink />
          <span className="lbl">{copy === "ok" ? "Copied" : copy === "fail" ? "Copy from address bar" : "Copy link"}</span>
        </button>
        <a
          className="btn btn-ghost btn-sm"
          href={reportUrl(target, fov)}
          target="_blank"
          rel="noopener"
          title="Printable report of what was measured and how far to trust it (new tab)"
          aria-label="Open the investigation report"
        >
          <IconReport />
          <span className="lbl">Report</span>
        </a>
        <button type="button" className="btn btn-sm" disabled={!onExportDiscovery} onClick={onExportDiscovery} title="Export an image card or GIF of this field" aria-label="Export image card or GIF">
          <IconImage />
          <span className="lbl-keep">Export</span>
        </button>
      </div>
      {phase.busy && (
        <div className={`progress-line ${phase.pct === null ? "is-indeterminate" : ""}`} aria-hidden="true">
          <div style={phase.pct === null ? undefined : { width: `${phase.pct}%` }} />
        </div>
      )}
    </div>
  );
}
