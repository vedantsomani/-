import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { CommandPalette, type PaletteAction } from "./components/CommandPalette";
import { DataFreshness, FieldAlerts } from "./components/FieldStatus";
import { fieldPhase } from "./lib/fieldPhase";
import { Header } from "./components/Header";
import { IconCollapse, IconExpand } from "./components/icons";
import { ItemDetails } from "./components/ItemDetails";
import { SelectionCard } from "./components/SelectionCard";
import { TargetBar } from "./components/TargetBar";
import { copyLink, reportUrl } from "./lib/share";
import { Timeline } from "./components/Timeline";
import { ViewControls } from "./components/ViewControls";
import { MODE_TABS } from "./lib/modes";
import { useFieldLoader } from "./hooks/useFieldLoader";
import { useImageCache } from "./hooks/useImageCache";
import { useOverlay } from "./hooks/useOverlay";
import { hasAnalysis } from "./lib/api";
import { BLINK_PANEL_ID, DETECTORS, EXAMPLE_TARGETS, TOUR_EXAMPLE, viewTabId } from "./lib/constants";
import { catwiseMarksFor, gaiaMarksFor, wiseMarksFor } from "./lib/overlay";
import { buildSequence, markersFor } from "./lib/sequence";
import {
  defaultDetector,
  defaultView,
  readView,
  viewToSearch,
  type NotebookSection,
  type Target,
  type ViewState,
} from "./lib/viewState";
import {
  skyToPix,
  snapNear,
  tanFromHeader,
  targetFromSnap,
  visibleCandidates,
} from "./lib/wcs";

// Heavy or rarely used parts load on demand, so the first paint of the viewer stays small.
const BlinkPlayer = lazy(() => import("./components/BlinkPlayer").then((m) => ({ default: m.BlinkPlayer })));
const DetectionsPanel = lazy(() => import("./components/DetectionsPanel").then((m) => ({ default: m.DetectionsPanel })));
const CandidateTriagePanel = lazy(() => import("./components/CandidateTriagePanel").then((m) => ({ default: m.CandidateTriagePanel })));
const EntryScreen = lazy(() => import("./components/EntryScreen").then((m) => ({ default: m.EntryScreen })));
const FingerprintPanel = lazy(() => import("./components/FingerprintPanel").then((m) => ({ default: m.FingerprintPanel })));
const PredictRevealPanel = lazy(() => import("./components/PredictRevealPanel").then((m) => ({ default: m.PredictRevealPanel })));
const ExplainPanel = lazy(() => import("./components/ExplainPanel").then((m) => ({ default: m.ExplainPanel })));
const BatchPage = lazy(() => import("./components/BatchPage").then((m) => ({ default: m.BatchPage })));
const AladinPanel = lazy(() => import("./components/AladinPanel").then((m) => ({ default: m.AladinPanel })));
const Tour = lazy(() => import("./components/Tour").then((m) => ({ default: m.Tour })));
const DiscoveryCardModal = lazy(() => import("./components/DiscoveryCardModal").then((m) => ({ default: m.DiscoveryCardModal })));
const KeyboardShortcutsModal = lazy(() =>
  import("./components/KeyboardShortcutsModal").then((m) => ({ default: m.KeyboardShortcutsModal })),
);

const APP_NAME = "GAMAS · The changing sky";

/** A click snaps to a candidate or Gaia star this close (grid pixels). */
const PICK_SNAP_PX = 2;

/** `?batch=<id>` (or `new`) shows the batch triage page instead of the viewer. */
const readBatch = () => new URLSearchParams(window.location.search).get("batch");

type InspectorTab = "verdict" | "spectrum" | "prediction" | "explain" | "sky";
const TABS: { id: InspectorTab; label: string }[] = [
  { id: "verdict", label: "Verdict" },
  { id: "spectrum", label: "Spectrum" },
  { id: "prediction", label: "Prediction" },
  { id: "explain", label: "Explain" },
  { id: "sky", label: "Sky" },
];

const isNarrow = () => window.matchMedia("(max-width: 1023px)").matches;

const Loading = ({ children = "Loading…" }: { children?: ReactNode }) => (
  <p className="help py-6 text-center" role="status">
    {children}
  </p>
);

const readWide = () => {
  try {
    return localStorage.getItem("gamas:wide") === "1";
  } catch {
    return false;
  }
};

export default function App() {
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [skyOpen, setSkyOpen] = useState(false);
  const [explainTab, setExplainTab] = useState(false);
  const [wide, setWide] = useState(readWide);
  const [view, setView] = useState<ViewState>(() => readView());
  const [index, setIndex] = useState(0);
  const [tourKey, setTourKey] = useState<number | null>(null);
  const [batchId, setBatchId] = useState<string | null>(readBatch);
  const { target, fov } = view;
  /** A pick made before the Gaia catalogue arrived; re-snapped once it does. */
  const rawPick = useRef<{ x: number; y: number; season: number } | null>(null);

  // Keep the URL in sync: new targets push history, view tweaks replace it.
  const update = useCallback((patch: Partial<ViewState>) => {
    setView((v) => {
      const next = { ...v, ...patch };
      const search = viewToSearch(next);
      if (search !== window.location.search) {
        const newTarget = patch.target !== undefined || patch.fov !== undefined;
        window.history[newTarget ? "pushState" : "replaceState"](null, "", `${window.location.pathname}${search}`);
      }
      return next;
    });
  }, []);

  // The inspector tab is the URL's `section` (observe/evidence both mean the verdict), plus Explain.
  const section = view.section;
  const tab: InspectorTab =
    section === "spectrum" || section === "prediction" || section === "sky"
      ? section
      : explainTab
        ? "explain"
        : "verdict";
  const setTab = useCallback(
    (t: InspectorTab) => {
      setExplainTab(t === "explain");
      if (t === "verdict" || t === "explain") update({ section: "observe" });
      else if (t === "spectrum") update({ section: "spectrum", fp: true });
      else update({ section: t as NotebookSection });
      if (isNarrow()) document.getElementById("inspector")?.scrollIntoView({ block: "start" });
    },
    [update],
  );
  /** On phones the image is above the inspector: bring it back after choosing something to look at. */
  const revealStage = useCallback(() => {
    if (isNarrow()) document.getElementById(BLINK_PANEL_ID)?.scrollIntoView({ block: "start" });
  }, []);

  useEffect(() => {
    const onPop = () => {
      setView(readView());
      setBatchId(readBatch());
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    document.title = batchId
      ? `Batch triage · ${APP_NAME}`
      : target
        ? `${target.label || "Custom position"} · ${APP_NAME}`
        : APP_NAME;
  }, [target, batchId]);

  useEffect(() => {
    try {
      localStorage.setItem("gamas:wide", wide ? "1" : "0");
    } catch {
      /* storage is optional */
    }
  }, [wide]);

  const { field, error, reconnecting, retry } = useFieldLoader(target, fov);

  // Fetch the viewer's code while the field request is in flight, so the image never waits on a chunk.
  const hasTarget = !!target;
  useEffect(() => {
    if (!hasTarget) return;
    void import("./components/BlinkPlayer");
    void import("./components/DetectionsPanel");
  }, [hasTarget]);

  const go = useCallback(
    (t: Target) => {
      setIndex(0);
      setBatchId(null);
      setExplainTab(false);
      rawPick.current = null;
      update({ target: t, det: defaultDetector(t), sel: null, cand: null, section: "observe" });
      window.scrollTo(0, 0);
    },
    [update],
  );

  /** Open a cached example exactly as its URL spells it (default view, example's own coordinates). */
  const openExample = useCallback((search: string) => {
    setIndex(0);
    setBatchId(null);
    setExplainTab(false);
    setView(readView(search));
    if (search !== window.location.search) window.history.pushState(null, "", `${window.location.pathname}${search}`);
    window.scrollTo(0, 0);
  }, []);

  const goHome = useCallback(() => {
    setIndex(0);
    setBatchId(null);
    setView(defaultView(null));
    if (window.location.search) window.history.pushState(null, "", window.location.pathname);
    window.scrollTo(0, 0);
  }, []);

  /** Show the batch page (`new` = the form) by pushing `?batch=<id>`. */
  const openBatch = useCallback((id: string) => {
    setBatchId(id);
    window.history.pushState(null, "", `${window.location.pathname}?batch=${encodeURIComponent(id)}`);
    window.scrollTo(0, 0);
  }, []);

  const startTour = useCallback(() => {
    openExample(TOUR_EXAMPLE.search);
    setTourKey(Date.now());
  }, [openExample]);

  // Global keys: Ctrl/Cmd+K or "/" opens the command palette; "?" the shortcuts (the player handles it in the viewer).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = e.target instanceof Element && e.target.closest("input,textarea,select,[contenteditable]");
      if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
        return;
      }
      if (e.ctrlKey || e.metaKey || e.altKey || typing || e.defaultPrevented) return;
      if (document.querySelector("[aria-modal=true]")) return;
      if (e.key === "/") {
        e.preventDefault();
        setPaletteOpen(true);
      } else if (e.key === "?" && !target) {
        e.preventDefault();
        setShortcutsOpen(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [target]);

  const seq = useMemo(() => buildSequence(field, view), [field, view]);
  const { items, stacksPending } = seq;
  const seasonLabel = useCallback(
    (s: number) => field?.seasons.find((x) => x.season === s)?.start.slice(0, 7) ?? String(s),
    [field],
  );
  const urls = useMemo(() => items.map((i) => i.url), [items]);
  const { images, failed, retry: retryImage, version } = useImageCache(urls);
  const safeIndex = items.length ? Math.min(index, items.length - 1) : 0;
  const item = items[safeIndex];
  const markers = useMemo(() => markersFor(field, view, item), [field, view, item]);
  const gridN = field?.grid.n ?? 0;
  const overlay = useOverlay(field?.key ?? null, view.gaia || view.wise || !!view.sel || tab === "verdict");
  const gaia = useMemo(() => (view.gaia ? gaiaMarksFor(overlay.data, item, gridN) : []), [view.gaia, overlay.data, item, gridN]);
  const catwise = useMemo(() => (view.gaia ? catwiseMarksFor(overlay.data, item, gridN) : []), [view.gaia, overlay.data, item, gridN]);
  const wise = useMemo(() => wiseMarksFor(view.wise ? overlay.data : null, item, gridN), [view.wise, overlay.data, item, gridN]);

  // Click to investigate: grid pixel <-> sky, and what the pick snaps to.
  const gridWcs = field?.grid.wcs;
  const wcs = useMemo(() => tanFromHeader(gridWcs), [gridWcs]);
  const gaiaSources = overlay.data?.catalog.ok ? overlay.data.sources : null;
  const catwiseSources = overlay.data?.catwise?.ok ? (overlay.data.catwise_sources ?? null) : null;
  const cands = useMemo(
    () => visibleCandidates(field && hasAnalysis(field) ? field.analysis.candidates : undefined, view.cand),
    [field, view.cand],
  );
  const selPix = useMemo(() => (wcs && view.sel ? skyToPix(wcs, view.sel.ra, view.sel.dec) : null), [wcs, view.sel]);
  const selSnap = useMemo(
    () => (wcs && selPix ? snapNear(wcs, selPix.x, selPix.y, 0.5, cands, gaiaSources, undefined, catwiseSources) : null),
    [wcs, selPix, cands, gaiaSources, catwiseSources],
  );
  const gaiaStatus = overlay.failed ? "failed" : gaiaSources ? "ok" : "loading";
  const onPick = useCallback(
    (x: number, y: number) => {
      if (!wcs) return;
      const season = item?.season ?? 0;
      rawPick.current = gaiaSources || overlay.failed ? null : { x, y, season };
      const s = snapNear(wcs, x, y, PICK_SNAP_PX, cands, gaiaSources, season, catwiseSources);
      update({ sel: { ra: s.ra, dec: s.dec } });
    },
    [wcs, item, cands, gaiaSources, catwiseSources, overlay.failed, update],
  );
  useEffect(() => {
    const p = rawPick.current;
    if (!p || !wcs || !(gaiaSources || overlay.failed)) return;
    rawPick.current = null;
    if (!gaiaSources) return;
    const s = snapNear(wcs, p.x, p.y, PICK_SNAP_PX, cands, gaiaSources, p.season, catwiseSources);
    update({ sel: { ra: s.ra, dec: s.dec } });
  }, [wcs, gaiaSources, catwiseSources, overlay.failed, cands, update]);

  const onCycleBand = useCallback((d: number) => update({ det: ((view.det - 1 + d + 6) % 6) + 1 }), [update, view.det]);
  const onCycleMode = useCallback(() => {
    const modes = ["stacks", "visits", "bins", "diff", "frames", "rgb"] as const;
    update({ mode: modes[(modes.indexOf(view.mode) + 1) % modes.length] });
  }, [update, view.mode]);
  const onToggleInvert = useCallback(() => update({ invert: !view.invert }), [update, view.invert]);
  const openShortcuts = useCallback(() => setShortcutsOpen(true), []);

  const phase = fieldPhase(field, error, reconnecting);
  if (stacksPending && phase.busy) phase.label += " · showing exposures";
  const analysed = !!field && hasAnalysis(field);
  const nCands = analysed ? field.analysis.candidates.length : 0;
  const hasAlert =
    !!error ||
    (!!field && field.status === "complete" && (field.total === 0 || (field.frames.length === 0 && Object.keys(field.failed).length > 0)));

  // ---- Command palette actions ----
  const paletteActions = useMemo<PaletteAction[]>(() => {
    if (!paletteOpen) return [];
    const a: PaletteAction[] = [];
    if (target) {
      for (const m of MODE_TABS)
        a.push({
          id: `mode-${m.mode}`,
          group: "Image",
          label: `Show ${m.label}`,
          keywords: `mode ${m.mode}`,
          hint: view.mode === m.mode ? "current" : undefined,
          run: () => update({ mode: m.mode }),
        });
      for (const d of DETECTORS)
        a.push({
          id: `det-${d.id}`,
          group: "Detector",
          label: `Detector D${d.id} · ${d.band}`,
          keywords: `d${d.id} band`,
          hint: view.det === d.id ? "current" : undefined,
          run: () => update({ det: d.id, bin: 0, pair: null }),
        });
      const toggles: [keyof ViewState, string, string][] = [
        ["marks", "Detections", "markers"],
        ["gaia", "Catalogue stars (Gaia, CatWISE)", "gaia catwise"],
        ["flags", "Flagged pixels", "flags mask"],
        ["wise", "AllWISE sources", "allwise"],
        ["invert", "Invert", "negative"],
      ];
      for (const [k, label, kw] of toggles)
        a.push({
          id: `tog-${k}`,
          group: "Overlays",
          label: `${view[k] ? "Hide" : "Show"} ${label.toLowerCase()}`,
          keywords: `${kw} toggle overlay`,
          hint: view[k] ? "on" : "off",
          run: () => update({ [k]: !view[k] } as Partial<ViewState>),
        });
      for (const t of TABS)
        a.push({ id: `tab-${t.id}`, group: "Inspector", label: `Open ${t.label}`, keywords: "panel section", run: () => setTab(t.id) });
      a.push(
        { id: "copy", group: "Field", label: "Copy link to this view", keywords: "share url", run: () => void copyLink() },
        { id: "report", group: "Field", label: "Open the investigation report", keywords: "download print", run: () => window.open(reportUrl(target, fov), "_blank", "noopener") },
        { id: "export", group: "Field", label: "Export image card or GIF", keywords: "download png", run: () => setExportOpen(true) },
        {
          id: "check",
          group: "Field",
          label: "Check for new observations",
          keywords: "update archive refresh",
          run: () => {
            setTab("verdict");
            window.setTimeout(() => document.querySelector<HTMLButtonElement>("[data-check-new]")?.click(), 50);
          },
        },
      );
    }
    for (const ex of EXAMPLE_TARGETS)
      a.push({ id: `ex-${ex.id}`, group: "Examples", label: ex.name, keywords: ex.tag, hint: ex.tag, run: () => openExample(ex.search) });
    a.push(
      { id: "batch", group: "Go to", label: "Batch triage of a target list", keywords: "list csv", run: () => openBatch("new") },
      { id: "tour", group: "Go to", label: "Guided tour", keywords: "help start", run: startTour },
      { id: "keys", group: "Go to", label: "Keyboard shortcuts", keywords: "help keys", hint: "?", run: () => setShortcutsOpen(true) },
      { id: "home", group: "Go to", label: "Home", keywords: "landing start", run: goHome },
    );
    return a;
  }, [paletteOpen, target, view, fov, update, setTab, openExample, openBatch, startTour, goHome]);

  const inspectorBody = target && (
    <>
      {tab === "verdict" && (
        <div data-tour="verdict" className="flex flex-col gap-4">
          {analysed ? (
            <Suspense fallback={<Loading />}>
              <DetectionsPanel
                analysis={field.analysis}
                seasonLabel={seasonLabel}
                catalogPm={target.pm}
                view={view}
                update={(patch) => {
                  update(patch);
                  revealStage();
                }}
                overlay={overlay.data}
              />
              <details className="inspector-block">
                <summary className="cursor-pointer text-[13px] font-semibold">Your review</summary>
                <div className="mt-3">
                  <CandidateTriagePanel
                    key={field.key}
                    fieldKey={field.key}
                    candidates={field.analysis.candidates}
                    activeCandidateId={view.cand}
                    onSelectCandidate={(cand) => update({ cand })}
                  />
                </div>
              </details>
            </Suspense>
          ) : (
            <div className="flex flex-col gap-3" aria-busy="true">
              <div className="h-7 w-40 rounded bg-ink-800" />
              <div className="h-4 w-full rounded bg-ink-900" />
              <div className="h-4 w-4/5 rounded bg-ink-900" />
              <p className="help">The verdict appears once every exposure is in and the seasons are compared. {phase.label}</p>
            </div>
          )}
          {field && item && (
            <details className="inspector-block">
              <summary className="cursor-pointer text-[13px] font-semibold">This image</summary>
              <div className="mt-3">
                <ItemDetails item={item} field={field} />
              </div>
            </details>
          )}
          {field && (
            <div className="inspector-block">
              <DataFreshness field={field} onRetry={retry} />
            </div>
          )}
        </div>
      )}
      {tab === "spectrum" && (
        <div data-tour="spectrum">
          <Suspense fallback={<Loading />}>
            <FingerprintPanel target={target} fov={fov} open={view.fp} onOpenChange={(fp) => update({ fp })} seasonLabel={seasonLabel} />
          </Suspense>
        </div>
      )}
      {tab === "prediction" && (
        <div data-tour="predict">
          <Suspense fallback={<Loading />}>
            <PredictRevealPanel target={target} fov={fov} />
          </Suspense>
        </div>
      )}
      {tab === "explain" &&
        (field && analysed ? (
          <Suspense fallback={<Loading />}>
            <ExplainPanel
              key={`${field.key}:${view.cand ?? "target"}`}
              fieldKey={field.key}
              candidateId={view.cand ?? "target"}
              onSelectRef={(ref) => {
                update({ mode: "diff", det: ref.detector, pair: [ref.season_a, ref.season_b], diffCombined: true });
                revealStage();
              }}
            />
          </Suspense>
        ) : (
          <p className="empty-note">The explanation is written from the measurements once the field is analysed.</p>
        ))}
      {tab === "sky" && (
        <div className="flex flex-col gap-3">
          <div>
            <h2 className="section-title">Sky context</h2>
            <p className="section-lede">
              The same field in external surveys (DSS, 2MASS, AllWISE) with the SPHEREx field outline. Loads Aladin Lite and
              survey tiles from IRSA.
            </p>
          </div>
          {!skyOpen ? (
            <button className="btn btn-primary self-start" onClick={() => setSkyOpen(true)}>
              Load sky context
            </button>
          ) : (
            <Suspense fallback={<Loading>Loading the sky atlas…</Loading>}>
              <AladinPanel ra={target.ra} dec={target.dec} fovArcmin={fov} onRecenter={(ra, dec) => go({ ra, dec, label: "" })} />
            </Suspense>
          )}
        </div>
      )}
      <p className="help border-t border-line pt-3 text-ink-500">
        Data: NASA SPHEREx via NASA/IPAC IRSA. Catalogues: Gaia DR3, CatWISE2020, AllWISE. Detections are candidates, not
        confirmed discoveries.
      </p>
    </>
  );

  return (
    <div className={`app ${target && !batchId ? "is-viewer" : ""}`}>
      <a href="#main" className="skip-link">
        Skip to content
      </a>
      <Header
        showSearch={!!target || !!batchId}
        fov={fov}
        onFovChange={(f) => update({ fov: f })}
        onTarget={go}
        onOpenExample={openExample}
        onStartTour={startTour}
        onHome={goHome}
        onOpenPalette={() => setPaletteOpen(true)}
        onOpenBatch={() => openBatch("new")}
      />
      {batchId ? (
        <main id="main" tabIndex={-1} className="page outline-none">
          <Suspense fallback={<Loading />}>
            <BatchPage id={batchId} onStarted={openBatch} onHome={goHome} onOpenView={openExample} />
          </Suspense>
        </main>
      ) : !target ? (
        <main id="main" tabIndex={-1} className="outline-none">
          <Suspense fallback={<div className="min-h-[80vh]" />}>
          <EntryScreen
            fov={fov}
            onFovChange={(f) => update({ fov: f })}
            onTarget={go}
            onOpenExample={openExample}
            onStartTour={startTour}
            onOpenBatch={() => openBatch("new")}
          />
          </Suspense>
        </main>
      ) : (
        <main id="main" tabIndex={-1} className={`viewer outline-none ${wide ? "is-wide" : ""}`}>
          <div className="stage-col">
            <TargetBar target={target} fov={fov} phase={phase} onExportDiscovery={items.length ? () => setExportOpen(true) : undefined} />
            {field && field.frames.length > 0 ? (
              <ViewControls view={view} field={field} seq={seq} seasonLabel={seasonLabel} update={update} overlay={overlay} />
            ) : (
              !hasAlert && <div className="stage-toolbar stage-toolbar-placeholder" aria-hidden="true" />
            )}
            <section id={BLINK_PANEL_ID} aria-labelledby={viewTabId(view.mode)} aria-label="SPHEREx images" className="stage">
              {field && items.length > 0 ? (
                <Suspense fallback={<div className="stage-empty"><div className="stage-skeleton" /></div>}>
                  <BlinkPlayer
                    key={`${field.key}:${items[0]?.kind}`}
                    items={items}
                    images={images}
                    imagesVersion={version}
                    failedImages={failed}
                    onRetryImage={retryImage}
                    gridN={field.grid.n}
                    pixscaleArcsec={field.grid.pixscale_arcsec}
                    index={safeIndex}
                    onIndex={setIndex}
                    invert={view.invert}
                    onToggleInvert={onToggleInvert}
                    active
                    pm={target.pm}
                    targetCenter={field.target}
                    defaultSpeed={items[0]?.kind === "frame" ? 4 : 1}
                    markers={markers}
                    gaia={gaia}
                    catwise={catwise}
                    wise={wise}
                    selected={selPix}
                    onPick={wcs ? onPick : undefined}
                    selectionCard={
                      selSnap && (
                        <SelectionCard
                          snap={selSnap}
                          gaiaStatus={gaiaStatus}
                          onInvestigate={() => go(targetFromSnap(selSnap))}
                          onClear={() => {
                            rawPick.current = null;
                            update({ sel: null });
                          }}
                        />
                      )
                    }
                    onCycleBand={onCycleBand}
                    onCycleMode={onCycleMode}
                    onOpenShortcuts={openShortcuts}
                  />
                  <div className="timeline-dock">
                    <Timeline items={items} seasons={field.seasons} current={safeIndex} onSelect={setIndex} />
                  </div>
                </Suspense>
              ) : hasAlert ? (
                <div className="stage-alerts">
                  <FieldAlerts field={field} error={error} onRetry={retry} onOpenExample={openExample} />
                </div>
              ) : (
                <div className="stage-empty" role="status">
                  <div className="stage-skeleton" />
                  <p>
                    {field?.status === "processing" || field?.status === "searching" || field?.status === "pending"
                      ? "The first visit to a position downloads every exposure from IRSA (a few minutes); later visits are instant."
                      : "Preparing the field…"}
                  </p>
                </div>
              )}
            </section>
          </div>
          <aside id="inspector" className="inspector" aria-label="Inspector">
            <div className="inspector-head">
              <div className="seg inspector-tabs" role="tablist" aria-label="Inspector sections">
                {TABS.map((t) => (
                  <button
                    key={t.id}
                    id={`inspector-tab-${t.id}`}
                    role="tab"
                    type="button"
                    aria-selected={tab === t.id}
                    aria-controls="inspector-panel"
                    tabIndex={tab === t.id ? 0 : -1}
                    onClick={() => setTab(t.id)}
                    onKeyDown={(e) => {
                      const i = TABS.findIndex((x) => x.id === tab);
                      const j = e.key === "ArrowRight" ? (i + 1) % TABS.length : e.key === "ArrowLeft" ? (i + TABS.length - 1) % TABS.length : -1;
                      if (j < 0) return;
                      e.preventDefault();
                      setTab(TABS[j].id);
                      document.getElementById(`inspector-tab-${TABS[j].id}`)?.focus();
                    }}
                  >
                    {t.label}
                    {t.id === "verdict" && nCands > 0 && <span className="count">{nCands}</span>}
                  </button>
                ))}
              </div>
              <button
                type="button"
                className="btn btn-ghost btn-sm btn-icon inspector-expand"
                onClick={() => setWide((w) => !w)}
                aria-pressed={wide}
                aria-label={wide ? "Narrow the inspector" : "Widen the inspector"}
                title={wide ? "Narrow the inspector" : "Widen the inspector for charts and tables"}
              >
                {wide ? <IconCollapse /> : <IconExpand />}
              </button>
            </div>
            <div id="inspector-panel" role="tabpanel" aria-labelledby={`inspector-tab-${tab}`} className="inspector-body">
              {inspectorBody}
            </div>
          </aside>
        </main>
      )}

      {paletteOpen && (
        <CommandPalette onClose={() => setPaletteOpen(false)} actions={paletteActions} view={target ? view : null} update={update} />
      )}
      {shortcutsOpen && (
        <Suspense fallback={null}>
          <KeyboardShortcutsModal open={shortcutsOpen} onClose={() => setShortcutsOpen(false)} />
        </Suspense>
      )}
      {target && exportOpen && (
        <Suspense fallback={null}>
          <DiscoveryCardModal
            open={exportOpen}
            onClose={() => setExportOpen(false)}
            target={target}
            verdict={analysed ? field.analysis.target.summary : undefined}
            pmMeasured={
              analysed && field.analysis.target.pmra_masyr !== undefined && field.analysis.target.pmdec_masyr !== undefined
                ? {
                    pmra: field.analysis.target.pmra_masyr,
                    pmdec: field.analysis.target.pmdec_masyr,
                    total: Math.hypot(field.analysis.target.pmra_masyr, field.analysis.target.pmdec_masyr),
                  }
                : null
            }
            itemA={items.find((i) => i.kind !== "diff")}
            itemB={[...items].reverse().find((i) => i.kind !== "diff")}
            imageA={images.get(items.find((i) => i.kind !== "diff")?.url ?? "")}
            imageB={images.get([...items].reverse().find((i) => i.kind !== "diff")?.url ?? "")}
          />
        </Suspense>
      )}
      {tourKey !== null && (
        <Suspense fallback={null}>
          <Tour
            key={tourKey}
            onClose={() => setTourKey(null)}
            onPatch={(patch) => {
              update(patch);
              if (patch.fp) setTab("spectrum");
            }}
            onSection={(anchor) => {
              if (anchor === "verdict") setTab("verdict");
              else if (anchor === "spectrum") setTab("spectrum");
              else if (anchor === "predict") setTab("prediction");
            }}
          />
        </Suspense>
      )}
    </div>
  );
}
