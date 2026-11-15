import { useReducedMotion } from "../hooks/useReducedMotion";
import { useModalFocus } from "../hooks/useModalFocus";
import { useEffect, useRef, useState } from "react";
import { formatDec, formatRa } from "../lib/coords";
import { createAnimatedGif } from "../lib/gifEncoder";
import type { BlinkItem } from "../lib/items";
import type { Target } from "../lib/viewState";
import { IconClose, IconDownload } from "./icons";

interface Props {
  open: boolean;
  onClose: () => void;
  target: Target;
  itemA?: BlinkItem | null;
  itemB?: BlinkItem | null;
  imageA?: HTMLImageElement | null;
  imageB?: HTMLImageElement | null;
  classification?: string | null;
  verdict?: string | null;
  pmMeasured?: { pmra: number; pmdec: number; total: number } | null;
}

export function DiscoveryCardModal({
  open,
  onClose,
  target,
  itemA,
  itemB,
  imageA,
  imageB,
  classification,
  verdict,
  pmMeasured,
}: Props) {
  const reducedMotion = useReducedMotion();
  const [previewPlaying, setPreviewPlaying] = useState(true);
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalFocus(dialogRef, open, onClose);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [downloadingGif, setDownloadingGif] = useState(false);
  const [blinkPreviewIdx, setBlinkPreviewIdx] = useState(0);

  // Animated blink in the preview modal
  useEffect(() => {
    if (!open || reducedMotion || !previewPlaying) return;
    const interval = setInterval(() => {
      setBlinkPreviewIdx((prev) => (prev === 0 ? 1 : 0));
    }, 600);
    return () => clearInterval(interval);
  }, [open, reducedMotion, previewPlaying]);

  // Draw 1200x630 card on canvas
  const drawCard = (): HTMLCanvasElement | null => {
    const canvas = canvasRef.current || document.createElement("canvas");
    canvas.width = 1200;
    canvas.height = 630;
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;

    // Background: Deep space navy --bg (#0b1114)
    ctx.fillStyle = "#0b1114";
    ctx.fillRect(0, 0, 1200, 630);

    // Outer border: --rule (#2b3a40)
    ctx.strokeStyle = "#2b3a40";
    ctx.lineWidth = 2;
    ctx.strokeRect(16, 16, 1168, 598);

    // Top Header: Space Apps Challenge branding (Strictly NO NASA meatball/worm per Rule 4)
    // Mini Space Apps Challenge logo icon
    ctx.save();
    ctx.fillStyle = "#b7e2cb";
    ctx.beginPath();
    ctx.arc(52, 54, 8, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#dfb48e";
    ctx.beginPath();
    ctx.arc(68, 48, 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();

    ctx.fillStyle = "#dfb48e"; // --hi
    ctx.font = "bold 15px DM Sans, sans-serif";
    ctx.fillText("GAMAS / OBSERVATION RECORD", 86, 55);

    ctx.fillStyle = "#9baab0"; // --mute
    ctx.font = "13px DM Sans, sans-serif";
    ctx.fillText(
      "CHALLENGE: PLANET X AND SPHEREX · CITIZEN SCIENCE VERIFICATION",
      86,
      75,
    );

    // Thin header rule
    ctx.strokeStyle = "#2b3a40";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(44, 94);
    ctx.lineTo(1156, 94);
    ctx.stroke();

    // Target Name in condensed 800 display, huge
    ctx.fillStyle = "#ffffff";
    ctx.font = "800 44px DM Sans, sans-serif";
    const titleText = target.label || "Candidate Infrared Mover";
    ctx.fillText(titleText, 44, 148);

    // Coordinates fact row
    ctx.fillStyle = "#9baab0";
    ctx.font = "15px DM Sans, sans-serif";
    ctx.fillText("Coordinates:", 44, 178);

    ctx.fillStyle = "#eef3ff";
    ctx.font = "bold 15px DM Sans, monospace";
    const coordText = `RA ${formatRa(target.ra)} (${target.ra.toFixed(4)}°)   Dec ${formatDec(target.dec)} (${target.dec >= 0 ? "+" : ""}${target.dec.toFixed(4)}°)`;
    ctx.fillText(coordText, 140, 178);

    // 4px Band-Gradient line separating header from main body
    const bandGrad = ctx.createLinearGradient(44, 196, 1156, 196);
    bandGrad.addColorStop(0, "#5b8cff"); // D1
    bandGrad.addColorStop(0.18, "#3fc6e8"); // D2
    bandGrad.addColorStop(0.36, "#4fe0a4"); // D3
    bandGrad.addColorStop(0.6, "#d9e94a"); // D4
    bandGrad.addColorStop(0.82, "#ffae4a"); // D5
    bandGrad.addColorStop(1.0, "#ff6b81"); // D6

    ctx.fillStyle = bandGrad;
    ctx.fillRect(44, 196, 1112, 4);

    // Dual Thumbnails: Epoch 1 vs Epoch 2
    const boxSize = 220;
    const boxY = 224;

    // Frame A Box
    ctx.fillStyle = "#111b20";
    ctx.fillRect(44, boxY, boxSize, boxSize);
    ctx.strokeStyle = "#2b3a40";
    ctx.lineWidth = 1;
    ctx.strokeRect(44, boxY, boxSize, boxSize);

    if (imageA) {
      try {
        ctx.drawImage(imageA, 44, boxY, boxSize, boxSize);
      } catch {
        // Fallback
      }
    }

    /* Target position is not inferred from an arbitrary image. */
    // Thin --hi ring around candidate center on Epoch 1
    ctx.strokeStyle = "#dfb48e";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    // No source position inferred from the image centre.
    ctx.stroke();

    ctx.fillStyle = "#eef3ff";
    ctx.font = "bold 13px DM Sans, sans-serif";
    ctx.fillText(
      itemA?.title ? `Epoch 1: ${itemA.title}` : "Epoch 1 (Baseline Stack)",
      44,
      boxY + boxSize + 22,
    );

    // Frame B Box
    ctx.fillStyle = "#111b20";
    ctx.fillRect(284, boxY, boxSize, boxSize);
    ctx.strokeStyle = "#2b3a40";
    ctx.lineWidth = 1;
    ctx.strokeRect(284, boxY, boxSize, boxSize);

    if (imageB) {
      try {
        ctx.drawImage(imageB, 284, boxY, boxSize, boxSize);
      } catch {
        // Fallback
      }
    }

    // Thin --hi ring around candidate center on Epoch 2
    ctx.strokeStyle = "#dfb48e";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    // No source position inferred from the image centre.
    ctx.stroke();

    ctx.fillStyle = "#eef3ff";
    ctx.font = "bold 13px DM Sans, sans-serif";
    ctx.fillText(
      itemB?.title ? `Epoch 2: ${itemB.title}` : "Epoch 2 (Follow-up Stack)",
      284,
      boxY + boxSize + 22,
    );

    // Right Panel: Astrometric & Spectral Discovery Properties
    const rightX = 540;

    ctx.fillStyle = "#b7e2cb"; // --pen
    ctx.font = "bold 17px DM Sans, sans-serif";
    ctx.fillText("ASTROMETRIC & SPECTRAL VERIFICATION", rightX, 245);

    // Proper Motion vector
    const pm =
      pmMeasured ||
      (target.pm
        ? {
            pmra: target.pm.pmra,
            pmdec: target.pm.pmdec,
            total: Math.hypot(target.pm.pmra, target.pm.pmdec),
          }
        : null);

    ctx.fillStyle = "#9baab0";
    ctx.font = "14px DM Sans, sans-serif";
    ctx.fillText(
      pmMeasured ? "Measured Proper Motion:" : "Supplied Catalogue Motion:",
      rightX,
      280,
    );

    ctx.fillStyle = "#ffffff";
    ctx.font = "bold 20px DM Sans, monospace";
    if (pm) {
      const pa = Math.round(
        ((Math.atan2(pm.pmra, pm.pmdec) * 180) / Math.PI + 360) % 360,
      );
      ctx.fillText(
        `μ = ${(pm.total / 1000).toFixed(2)}″/yr @ ${pa}°`,
        rightX,
        308,
      );
      ctx.fillStyle = "#9baab0";
      ctx.font = "13px DM Sans, monospace";
      ctx.fillText(
        `(μ_α* = ${pm.pmra.toFixed(1)} mas/yr,  μ_δ = ${pm.pmdec.toFixed(1)} mas/yr)`,
        rightX,
        330,
      );
    } else {
      ctx.fillText("Motion not available", rightX, 308);
    }

    // Verdict / Status
    ctx.fillStyle = "#9baab0";
    ctx.font = "14px DM Sans, sans-serif";
    ctx.fillText("Centroid Verification Check:", rightX, 375);

    ctx.fillStyle = "#5ee6a0"; // --ok
    ctx.font = "bold 16px DM Sans, sans-serif";
    const statusText = verdict || "Analysis not available";
    ctx.fillText(statusText, rightX, 400);

    // Spectral Classification
    if (classification) {
      ctx.fillStyle = "#9baab0";
      ctx.font = "14px DM Sans, sans-serif";
      ctx.fillText("Deblended Spectral Classification:", rightX, 440);

      ctx.fillStyle = "#dfb48e"; // --hi
      ctx.font = "bold 16px DM Sans, sans-serif";
      ctx.fillText(classification, rightX, 465);
    }

    // Instrument parameters
    ctx.fillStyle = "#9baab0";
    ctx.font = "13px DM Sans, sans-serif";
    ctx.fillText(
      "Instrument: SPHEREx 6 H2RG Detectors (0.75–5.0 µm, 102 channels)",
      rightX,
      502,
    );

    // Footer divider
    ctx.strokeStyle = "#2b3a40";
    ctx.beginPath();
    ctx.moveTo(44, 538);
    ctx.lineTo(1156, 538);
    ctx.stroke();

    // Footer Attribution & Compliance
    ctx.fillStyle = "#9baab0";
    ctx.font = "12px DM Sans, sans-serif";
    ctx.fillText(
      "Built with GAMAS · SPHEREx Time Machine · Team GAMAS",
      44,
      568,
    );

    ctx.fillStyle = "#9baab0";
    ctx.font = "10px DM Sans, sans-serif";
    ctx.fillText(
      "Data: NASA SPHEREx mission data via IRSA / IPAC / Caltech. Independent NASA Space Apps 2026 project. Not endorsed by NASA.",
      44,
      590,
    );

    ctx.fillStyle = "#dfb48e";
    ctx.font = "bold 13px DM Sans, sans-serif";
    ctx.fillText("spaceappschallenge.org", 980, 575);

    return canvas;
  };

  const handleDownload = () => {
    setDownloading(true);
    try {
      const canvas = drawCard();
      if (!canvas) return;
      const url = canvas.toDataURL("image/png");
      const a = document.createElement("a");
      const slug = (target.label || "target")
        .toLowerCase()
        .replace(/[^a-z0-9]/g, "_");
      a.download = `gamas-discovery-${slug}-1200x630.png`;
      a.href = url;
      a.click();
    } finally {
      setDownloading(false);
    }
  };

  const handleDownloadGif = () => {
    if (!imageA && !imageB) return;
    setDownloadingGif(true);
    try {
      const size = 360;
      const c1 = document.createElement("canvas");
      c1.width = size;
      c1.height = size;
      const ctx1 = c1.getContext("2d");
      if (ctx1) {
        ctx1.fillStyle = "#0b1114";
        ctx1.fillRect(0, 0, size, size);
        if (imageA) ctx1.drawImage(imageA, 0, 0, size, size);
        ctx1.fillStyle = "rgba(6, 11, 23, 0.85)";
        ctx1.fillRect(0, size - 44, size, 44);
        ctx1.fillStyle = "#dfb48e";
        ctx1.font = "bold 12px DM Sans, sans-serif";
        ctx1.fillText(target.label || "Candidate Mover", 10, size - 26);
        ctx1.fillStyle = "#9baab0";
        ctx1.font = "10px DM Sans, monospace";
        ctx1.fillText(
          itemA?.title ? `Epoch 1: ${itemA.title}` : "Epoch 1",
          10,
          size - 10,
        );
      }

      const c2 = document.createElement("canvas");
      c2.width = size;
      c2.height = size;
      const ctx2 = c2.getContext("2d");
      if (ctx2) {
        ctx2.fillStyle = "#0b1114";
        ctx2.fillRect(0, 0, size, size);
        if (imageB) ctx2.drawImage(imageB, 0, 0, size, size);
        else if (imageA) ctx2.drawImage(imageA, 0, 0, size, size);
        ctx2.fillStyle = "rgba(6, 11, 23, 0.85)";
        ctx2.fillRect(0, size - 44, size, 44);
        ctx2.fillStyle = "#dfb48e";
        ctx2.font = "bold 12px DM Sans, sans-serif";
        ctx2.fillText(target.label || "Candidate Mover", 10, size - 26);
        ctx2.fillStyle = "#9baab0";
        ctx2.font = "10px DM Sans, monospace";
        ctx2.fillText(
          itemB?.title ? `Epoch 2: ${itemB.title}` : "Epoch 2",
          10,
          size - 10,
        );
      }

      const gifBlob = createAnimatedGif(
        [
          { canvas: c1, delayMs: 500 },
          { canvas: c2, delayMs: 500 },
        ],
        size,
        size,
      );

      const url = URL.createObjectURL(gifBlob);
      const a = document.createElement("a");
      const slug = (target.label || "target")
        .toLowerCase()
        .replace(/[^a-z0-9]/g, "_");
      a.download = `gamas-blink-${slug}.gif`;
      a.href = url;
      a.click();
      URL.revokeObjectURL(url);
    } finally {
      setDownloadingGif(false);
    }
  };

  if (!open) return null;

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby="discovery-card-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 p-4"
    >
      <div className="relative flex max-h-[90vh] w-full max-w-4xl flex-col overflow-hidden rounded-[var(--radius-panel)] border border-[var(--rule)] bg-[var(--plate)] shadow-[0_24px_64px_rgb(0_0_0/0.55)]">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-[var(--rule)] px-6 py-4">
          <div>
            <h3
              id="discovery-card-title"
              className="font-display text-lg font-bold text-white tracking-tight"
            >
              Export Discovery Card (1200×630 PNG)
            </h3>
            <p className="text-xs text-[var(--mute)]">
              Export the selected images and available measurements.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-[var(--radius-pill)] p-1.5 text-[var(--mute)] hover:bg-[var(--bg)] hover:text-white transition-colors"
            aria-label="Close discovery card modal"
          >
            <IconClose />
          </button>
        </div>

        {/* Card Interactive Preview */}
        <div className="flex-1 overflow-y-auto p-6">
          <div className="mx-auto flex flex-col gap-4 rounded-[var(--radius-panel)] border border-[var(--rule)] bg-[#0b1114] p-5 shadow-inner">
            <div className="flex items-center justify-between border-b border-[var(--rule)] pb-3">
              <div>
                <span className="text-xs font-semibold text-ink-300">
                  NASA Space Apps Challenge 2026
                </span>
                <h4 className="font-display text-2xl font-bold text-white mt-0.5">
                  {target.label || "Candidate Infrared Mover"}
                </h4>
                <p className="tabular text-xs text-[var(--mute)]">
                  RA {formatRa(target.ra)} · Dec {formatDec(target.dec)}
                </p>
              </div>
              <div className="text-right">
                <span className="rounded-[var(--radius-pill)] border border-[var(--pen)]/40 bg-[var(--pen)]/10 px-2.5 py-0.5 text-xs font-display font-bold text-[var(--pen)]">
                  1200 × 630 PNG
                </span>
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 items-center pt-2">
              <div className="flex flex-col gap-2">
                <div className="aspect-square max-h-[220px] w-full max-w-[220px] mx-auto overflow-hidden rounded-[var(--radius-panel)] border border-[var(--rule)] bg-[var(--plate)] grid place-items-center relative">
                  {blinkPreviewIdx === 0 && imageA ? (
                    <img
                      src={imageA.src}
                      alt="Epoch 1"
                      className="h-full w-full object-cover"
                    />
                  ) : imageB ? (
                    <img
                      src={imageB.src}
                      alt="Epoch 2"
                      className="h-full w-full object-cover"
                    />
                  ) : (
                    <span className="text-xs text-[var(--mute)]">
                      Image preview
                    </span>
                  )}
                  <span className="absolute bottom-1 right-1 rounded-[var(--radius-pill)] bg-black/75 px-2 py-0.5 text-[10px] text-[var(--pen)] font-medium">
                    Blink: Epoch {blinkPreviewIdx + 1}
                  </span>
                </div>
                <p className="text-center text-xs text-[var(--mute)]">
                  {blinkPreviewIdx === 0
                    ? itemA?.title || "Epoch 1"
                    : itemB?.title || "Epoch 2"}
                </p>
                <div className="flex flex-wrap justify-center gap-2" role="group" aria-label="Preview observation dates">
                  <button className="btn btn-sm" aria-pressed={blinkPreviewIdx === 0} onClick={() => { setPreviewPlaying(false); setBlinkPreviewIdx(0); }}>Epoch 1</button>
                  <button className="btn btn-sm" aria-pressed={blinkPreviewIdx === 1} onClick={() => { setPreviewPlaying(false); setBlinkPreviewIdx(1); }}>Epoch 2</button>
                  {!reducedMotion && <button className="btn btn-sm" aria-pressed={previewPlaying} onClick={() => setPreviewPlaying((v) => !v)}>{previewPlaying ? "Pause" : "Blink"}</button>}
                </div>
              </div>

              <div className="flex flex-col gap-2.5 text-xs">
                <div>
                  <span className="text-[var(--mute)] font-medium text-[11px]">
                    Proper Motion
                  </span>
                  <p className="tabular text-sm text-white font-bold">
                    {pmMeasured
                      ? `${(pmMeasured.total / 1000).toFixed(2)}″/yr (${pmMeasured.total.toFixed(0)} mas/yr)`
                      : target.pm
                        ? `${(Math.hypot(target.pm.pmra, target.pm.pmdec) / 1000).toFixed(2)}″/yr`
                        : "Motion not available"}
                  </p>
                </div>

                <div>
                  <span className="text-[var(--mute)] font-medium text-[11px]">
                    Verification Verdict
                  </span>
                  <p className="text-[var(--ok)] font-medium">
                    {verdict || "Analysis not available"}
                  </p>
                </div>

                {classification && (
                  <div>
                    <span className="text-[var(--mute)] font-medium text-[11px]">
                      Spectral Type
                    </span>
                    <p className="text-[var(--pen)] font-semibold">
                      {classification}
                    </p>
                  </div>
                )}

                <div className="border-t border-[var(--rule)] pt-2 text-[11px] text-[var(--mute)]">
                  Data: NASA SPHEREx via IRSA / IPAC / Caltech. Candidate
                  interpretations require follow-up.
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Footer Actions */}
        <div className="flex flex-wrap items-center justify-between gap-4 border-t border-[var(--rule)] bg-[var(--bg)]/80 px-6 py-4">
          <p className="text-xs text-[var(--mute)]">
            Keep the comparison, coordinates, and available measurements together.
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={onClose}
              className="btn btn-quiet text-xs"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleDownloadGif}
              disabled={downloadingGif || (!imageA && !imageB)}
              className="btn btn-quiet text-xs flex items-center gap-1.5 hover:border-[var(--pen)] hover:text-white"
              title="Export 360×360 animated GIF of the blink comparison"
            >
              <IconDownload />
              <span>{downloadingGif ? "Encoding GIF…" : "Animated GIF"}</span>
            </button>
            <button
              type="button"
              onClick={handleDownload}
              disabled={downloading}
              className="btn btn-hi text-xs flex items-center gap-2"
            >
              <IconDownload />
              <span>
                {downloading ? "Rendering…" : "Download PNG (1200×630)"}
              </span>
            </button>
          </div>
        </div>

        {/* Hidden Canvas for crisp export */}
        <canvas ref={canvasRef} className="hidden" />
      </div>
    </div>
  );
}
