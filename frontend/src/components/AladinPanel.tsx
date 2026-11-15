import { useEffect, useRef, useState } from "react";
import type { AladinInstance } from "aladin-lite";

// Full HiPS URLs on IRSA's mirror: resolving CDS IDs needs the CDS MocServer, which
// has been unreachable/redirecting, and then Aladin shows nothing.
const IRSA_HIPS = "https://irsa.ipac.caltech.edu/data/hips/CDS";
const SURVEYS = [
  { id: `${IRSA_HIPS}/2MASS/Color`, label: "2MASS color (J,H,K)" },
  { id: `${IRSA_HIPS}/AllWISE/RGB-W4-W2-W1`, label: "AllWISE color" },
  { id: `${IRSA_HIPS}/AllWISE/W2`, label: "AllWISE W2 4.6 µm" },
  { id: `${IRSA_HIPS}/DSS2/color`, label: "DSS2 color (optical)" },
];

interface Props {
  ra: number;
  dec: number;
  fovArcmin: number;
  onRecenter?: (ra: number, dec: number) => void;
}

/** Inverse gnomonic projection: tangent-plane offsets (deg) -> RA/Dec (deg). */
function offsetToRaDec(
  ra0: number,
  dec0: number,
  xi: number,
  eta: number,
): [number, number] {
  const r = Math.PI / 180;
  const x = xi * r;
  const y = eta * r;
  const d0 = dec0 * r;
  const den = Math.cos(d0) - y * Math.sin(d0);
  const ra = ra0 * r + Math.atan2(x, den);
  const dec = Math.atan2(Math.sin(d0) + y * Math.cos(d0), Math.hypot(x, den));
  return [(((ra / r) % 360) + 360) % 360, dec / r];
}

export function AladinPanel({ ra, dec, fovArcmin, onRecenter }: Props) {
  const divRef = useRef<HTMLDivElement>(null);
  const aladinRef = useRef<AladinInstance | null>(null);
  const overlayRef = useRef<{
    removeAll(): void;
    add(s: unknown): void;
  } | null>(null);
  const libRef = useRef<typeof import("aladin-lite").default | null>(null);
  const [survey, setSurvey] = useState(SURVEYS[0].id);
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);

  // init once (Aladin Lite is ~1.5 MB incl. WASM, so load it lazily)
  useEffect(() => {
    let cancelled = false;
    import("aladin-lite")
      .then(async ({ default: A }) => {
        await A.init;
        if (cancelled || !divRef.current) return;
        libRef.current = A;
        aladinRef.current = A.aladin(divRef.current, {
          log: false,
          survey: SURVEYS[0].id,
          fov: (fovArcmin * 3) / 60,
          target: `${ra} ${dec}`,
          cooFrame: "ICRS",
          showReticle: true,
          showCooGridControl: false,
          showSettingsControl: false,
          showLayersControl: false,
          showFullscreenControl: true,
          showProjectionControl: false,
          showFrame: true,
        });
        const ov = A.graphicOverlay({ color: "#f5b942", lineWidth: 2 });
        aladinRef.current.addOverlay(ov);
        overlayRef.current = ov;
        setReady(true);
      })
      .catch((e) => setFailed(String(e)));
    return () => {
      cancelled = true;
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // follow the target and draw the blink field of view
  useEffect(() => {
    const al = aladinRef.current;
    const A = libRef.current;
    if (!ready || !al || !A || !overlayRef.current) return;
    al.gotoRaDec(ra, dec);
    al.setFoV((fovArcmin * 3) / 60);
    const h = fovArcmin / 120;
    const corners = [
      [-h, -h],
      [h, -h],
      [h, h],
      [-h, h],
    ].map(([x, y]) => offsetToRaDec(ra, dec, x, y));
    overlayRef.current.removeAll();
    overlayRef.current.add(A.polygon(corners));
  }, [ra, dec, fovArcmin, ready]);

  useEffect(() => {
    if (ready) aladinRef.current?.setImageSurvey(survey);
  }, [survey, ready]);

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-[13px] font-semibold">Survey</h3>
        <select
          value={survey}
          onChange={(e) => setSurvey(e.target.value)}
          className="rounded-md border border-ink-700 bg-ink-900 px-2 py-1 text-xs"
          aria-label="Background survey"
        >
          {SURVEYS.map((s) => (
            <option key={s.id} value={s.id}>
              {s.label}
            </option>
          ))}
        </select>
      </div>
      <div
        ref={divRef}
        className="aspect-square w-full overflow-hidden rounded-md border border-ink-800 bg-black"
      />
      {failed && (
        <p className="text-xs text-red-400">
          Aladin Lite failed to load: {failed}
        </p>
      )}
      {onRecenter && ready && (
        <button
          type="button"
          onClick={() => {
            const [r, d] = aladinRef.current!.getRaDec();
            onRecenter(r, d);
          }}
          className="self-start text-xs text-ink-300 underline decoration-ink-700 underline-offset-2 hover:text-accent"
        >
          Blink the position at Aladin's centre
        </button>
      )}
    </div>
  );
}
