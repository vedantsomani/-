import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type MouseEvent,
  type TouchEvent,
} from "react";
import { useImageCache } from "../hooks/useImageCache";
import { useReducedMotion } from "../hooks/useReducedMotion";
import {
  COLORMAP_OPTIONS,
  applyColormapToCanvas,
  computeImageHistogram,
  type ColormapId,
} from "../lib/colormaps";
import { detectorColor } from "../lib/constants";
import {
  gridPixToRaDec,
  pmOffsetArcsec,
  type ProperMotion,
} from "../lib/coords";
import type { BlinkItem } from "../lib/items";
import type { GaiaMark } from "../lib/overlay";
import type { Marker } from "../lib/sequence";
import { IconFullscreen, IconNext, IconPalette, IconPause, IconPlay, IconPrev, IconRuler } from "./icons";
import { Popover } from "./Popover";

interface Props {
  active?: boolean;
  items: BlinkItem[];
  images: Map<string, HTMLImageElement>;
  imagesVersion: number;
  failedImages?: Set<string>;
  onRetryImage?: (url: string) => void;
  gridN: number;
  pixscaleArcsec: number;
  index: number;
  onIndex: (i: number) => void;
  invert: boolean;
  onToggleInvert?: () => void;
  pm?: ProperMotion;
  targetCenter?: { ra: number; dec: number };
  defaultSpeed?: number;
  markers?: Marker[];
  gaia?: GaiaMark[];
  catwise?: GaiaMark[];
  wise?: GaiaMark[];
  selected?: { x: number; y: number } | null;
  onPick?: (x: number, y: number) => void;
  selectionCard?: ReactNode;
  onCycleBand?: (direction: 1 | -1) => void;
  onCycleMode?: () => void;
  onOpenShortcuts?: () => void;
}

/** White dashed ring with a dark halo (dark ring on inverted images) and a small label; never hidden by the blink. */

function drawSelection(
  ctx: CanvasRenderingContext2D,
  sel: { x: number; y: number },
  px: number,
  gridN: number,
  invert: boolean,
) {
  const x = (sel.x + 0.5) * px;

  const y = (gridN - 0.5 - sel.y) * px;

  const r = Math.max(px * 1.4, 11);

  ctx.save();

  ctx.beginPath();

  ctx.arc(x, y, r, 0, 2 * Math.PI);

  ctx.strokeStyle = invert ? "rgba(255,255,255,0.8)" : "rgba(0,0,0,0.7)";

  ctx.lineWidth = 4;

  ctx.stroke();

  ctx.setLineDash([4, 3]);

  ctx.strokeStyle = invert ? "#000" : "#fff";

  ctx.lineWidth = 1.75;

  ctx.stroke();

  ctx.setLineDash([]);

  ctx.font = "11px ui-monospace, monospace";

  ctx.lineWidth = 3;

  ctx.strokeStyle = invert ? "rgba(255,255,255,0.85)" : "rgba(0,0,0,0.8)";

  const tw = ctx.measureText("selected").width;

  const lx = x + r + 4 + tw > px * gridN - 2 ? x - r - 4 - tw : x + r + 4; // flip to the left near the right edge

  ctx.strokeText("selected", lx, y + 4);

  ctx.fillStyle = invert ? "#000" : "#fff";

  ctx.fillText("selected", lx, y + 4);

  ctx.restore();
}

/** Arrow (length ~ motion over 10 years) and a ″/yr label for a moving catalogue source at canvas (x, y). */

function drawMotion(
  ctx: CanvasRenderingContext2D,
  m: GaiaMark,
  x: number,
  y: number,
  r: number,
  px: number,
  size: number,

  pixscale: number,
  colour: string,
) {
  const [e, n] = m.pm as [number, number];

  const speed = m.speed as number;

  // east is left, north is up. Length: 8 px plus the distance covered in 10 years, kept on the canvas.

  const d = Math.hypot(e, n) || 1;

  const ux = -e / d;

  const uy = -n / d;

  const len = Math.min(8 + (speed * 10 * px) / pixscale, size * 0.6);

  ctx.strokeStyle = colour;

  ctx.fillStyle = colour;

  ctx.lineWidth = 1.5;

  const a0 = r + 1;

  const x1 = x + ux * (a0 + len);

  const y1 = y + uy * (a0 + len);

  ctx.beginPath();

  ctx.moveTo(x + ux * a0, y + uy * a0);

  ctx.lineTo(x1, y1);

  ctx.moveTo(x1 - ux * 6 + uy * 3.5, y1 - uy * 6 - ux * 3.5);

  ctx.lineTo(x1, y1);

  ctx.lineTo(x1 - ux * 6 - uy * 3.5, y1 - uy * 6 + ux * 3.5);

  ctx.stroke();

  const text = `${speed.toFixed(speed < 1 ? 2 : 1)}″/yr`;

  const tw = ctx.measureText(text).width;

  ctx.fillText(
    text,
    Math.min(Math.max(x1 + 5, 4), size - tw - 4),
    Math.min(Math.max(y1 - 4, 12), size - 4),
  );
}

/** Small orange diamonds for red / moving CatWISE sources (cyan rings are Gaia); movers get the same arrow. */

function drawCatwise(
  ctx: CanvasRenderingContext2D,
  marks: GaiaMark[],
  px: number,
  gridN: number,
  size: number,
  pixscale: number,
  invert: boolean,
) {
  const edge = invert ? "rgba(190,80,0,0.95)" : "rgba(255,160,60,0.95)";

  const r = Math.max(px * 0.55, 4);

  ctx.save();

  ctx.font = "10px ui-monospace, monospace";

  ctx.lineWidth = 1.5;

  for (const m of marks) {
    const x = (m.x + 0.5) * px;

    const y = (gridN - 0.5 - m.y) * px;

    ctx.strokeStyle = edge;

    ctx.beginPath();

    ctx.moveTo(x, y - r);

    ctx.lineTo(x + r, y);

    ctx.lineTo(x, y + r);

    ctx.lineTo(x - r, y);

    ctx.closePath();

    ctx.stroke();

    if (m.pm && m.speed) drawMotion(ctx, m, x, y, r, px, size, pixscale, edge);
  }

  ctx.restore();
}

function drawGaia(
  ctx: CanvasRenderingContext2D,
  marks: GaiaMark[],
  px: number,
  gridN: number,
  size: number,
  pixscale: number,
  invert: boolean,
) {
  const ring = invert ? "rgba(0,110,125,0.7)" : "rgba(110,200,210,0.65)";
  const strong = invert ? "rgba(0,100,115,1)" : "rgba(130,225,235,1)";
  const r = Math.max(px * 0.7, 3.5);
  ctx.save();
  ctx.font = "10px ui-monospace, monospace";
  for (const m of marks) {
    const x = (m.x + 0.5) * px;
    const y = (gridN - 0.5 - m.y) * px;
    ctx.strokeStyle = ring;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, 2 * Math.PI);
    ctx.stroke();
    if (!m.pm || !m.speed) continue;
    const [e, n] = m.pm;
    const d = Math.hypot(e, n) || 1;
    const ux = -e / d;
    const uy = -n / d;
    const len = Math.min(8 + (m.speed * 10 * px) / pixscale, size * 0.6);
    ctx.strokeStyle = strong;
    ctx.fillStyle = strong;
    ctx.lineWidth = 1.5;
    const a0 = r + 1;
    const x1 = x + ux * (a0 + len);
    const y1 = y + uy * (a0 + len);
    ctx.beginPath();
    ctx.moveTo(x + ux * a0, y + uy * a0);
    ctx.lineTo(x1, y1);
    ctx.moveTo(x1 - ux * 6 + uy * 3.5, y1 - uy * 6 - ux * 3.5);
    ctx.lineTo(x1, y1);
    ctx.lineTo(x1 - ux * 6 - uy * 3.5, y1 - uy * 6 + ux * 3.5);
    ctx.stroke();
    const text = `${m.speed.toFixed(m.speed < 1 ? 2 : 1)}″/yr`;
    const tw = ctx.measureText(text).width;
    ctx.fillText(
      text,
      Math.min(Math.max(x1 + 5, 4), size - tw - 4),
      Math.min(Math.max(y1 - 4, 12), size - 4),
    );
  }
  ctx.restore();
}

const MARK_COLOR: Record<Marker["kind"], string> = {
  mover: "#e5bb73",
  variable: "#ffd166",
  appeared: "#4ade80",
  vanished: "#c084fc",
  uncertain: "#8b93a7",
  fast: "#38bdf8",
};

function drawMarkers(
  ctx: CanvasRenderingContext2D,
  markers: Marker[],
  px: number,
  gridN: number,
) {
  for (const m of markers) {
    const x = (m.x + 0.5) * px;
    const y = (gridN - 0.5 - m.y) * px;
    const r = Math.max(px * 1.8, 8);
    ctx.save();
    ctx.strokeStyle = MARK_COLOR[m.kind];
    ctx.fillStyle = MARK_COLOR[m.kind];
    ctx.lineWidth = m.highlight ? 2.5 : 1.5;
    if (m.kind === "uncertain") ctx.setLineDash([3, 3]);
    ctx.beginPath();
    if (m.kind === "variable") {
      ctx.moveTo(x, y - r);
      ctx.lineTo(x + r, y);
      ctx.lineTo(x, y + r);
      ctx.lineTo(x - r, y);
      ctx.closePath();
    } else if (m.kind === "appeared" || m.kind === "vanished") {
      ctx.rect(x - r * 0.8, y - r * 0.8, r * 1.6, r * 1.6);
    } else {
      ctx.arc(x, y, r, 0, 2 * Math.PI);
    }
    ctx.stroke();

    if (m.kind === "mover" && m.pm) {
      const [e, n] = m.pm;
      const len = Math.hypot(e, n) || 1;
      const ux = -e / len;
      const uy = -n / len;
      const a0 = r + 2;
      const a1 = r + Math.max(px * 2.5, 14);
      ctx.beginPath();
      ctx.moveTo(x + ux * a0, y + uy * a0);
      ctx.lineTo(x + ux * a1, y + uy * a1);
      ctx.lineTo(
        x + ux * a1 - (ux * 6 - uy * 4),
        y + uy * a1 - (uy * 6 + ux * 4),
      );
      ctx.moveTo(x + ux * a1, y + uy * a1);
      ctx.lineTo(
        x + ux * a1 - (ux * 6 + uy * 4),
        y + uy * a1 - (uy * 6 - ux * 4),
      );
      ctx.stroke();
    }
    if (m.label) {
      ctx.font = "11px ui-monospace, monospace";
      ctx.fillText(m.label, x + r + 3, y - r);
    }
    ctx.restore();
  }
}

const SPEEDS = [0.5, 1, 2, 4, 8, 15];

export function BlinkPlayer({
  active = true,
  items,
  images,
  imagesVersion,
  failedImages,
  onRetryImage,
  gridN,
  pixscaleArcsec,
  index,
  onIndex,
  invert,
  onToggleInvert,
  pm,
  targetCenter,
  defaultSpeed = 4,
  markers,
  gaia,
  catwise,
  wise,
  selected,
  onPick,
  selectionCard,
  onCycleBand,
  onCycleMode,
  onOpenShortcuts,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const secondCanvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const playerContainerRef = useRef<HTMLDivElement>(null);
  const reduceMotion = useReducedMotion();

  const [playing, setPlaying] = useState(() => !reduceMotion);
  const [speed, setSpeed] = useState(defaultSpeed);
  const [size, setSize] = useState(560);
  const [compareMode, setCompareMode] = useState<
    "blink" | "side-by-side" | "wipe"
  >("blink");
  const [wipeRatio, setWipeRatio] = useState(0.5);
  const [isDraggingWipe, setIsDraggingWipe] = useState(false);
  const [selectedColormap, setSelectedColormap] = useState<ColormapId | null>(
    null,
  );
  const colormap: ColormapId =
    selectedColormap ?? (invert ? "inverted" : "greyscale");
  const [minClip, setMinClip] = useState(0);
  const [maxClip, setMaxClip] = useState(255);
  const [histogram, setHistogram] = useState<number[]>([]);
  const [hoverCoord, setHoverCoord] = useState<{ x: number; y: number } | null>(
    null,
  );
  const [isFullscreen, setIsFullscreen] = useState(false);

  // Pan & Zoom state
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [isDragging, setIsDragging] = useState(false);
  const [dragStart, setDragStart] = useState({ x: 0, y: 0 });
  const [touchDistance, setTouchDistance] = useState<number | null>(null);

  // Measurement Ruler state
  const [rulerActive, setRulerActive] = useState(false);
  const [rulerPoints, setRulerPoints] = useState<{
    start: { x: number; y: number } | null;
    current: { x: number; y: number } | null;
  }>({ start: null, current: null });

  const pickDown = useRef<{ x: number; y: number } | null>(null);
  const lastDrawn = useRef<string | null>(null);
  const lastDrawnSecondary = useRef<string | null>(null);

  const n = items.length;
  const item = items[index];
  const secondaryIndex = (index + 1) % Math.max(n, 1);
  const secondaryItem = n > 1 ? items[secondaryIndex] : null;

  // Overlays
  const overlayUrls = useMemo(
    () => items.flatMap((i) => (i.overlayUrl ? [i.overlayUrl] : [])),
    [items],
  );
  const { images: overlays, version: overlaysVersion } =
    useImageCache(overlayUrls);
  const overlayImg = item?.overlayUrl
    ? overlays.get(item.overlayUrl)
    : undefined;
  const secondaryOverlayImg = secondaryItem?.overlayUrl
    ? overlays.get(secondaryItem.overlayUrl)
    : undefined;

  // Measure the space the stage gives the image: square, as large as the box allows (two squares side by side).
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const measure = (width: number, height: number) => {
      const h = height > 40 ? height : width;
      const targetSize =
        compareMode === "side-by-side"
          ? Math.min(h, Math.floor((width - 12) / 2))
          : Math.min(h, width);
      setSize(Math.max(80, Math.floor(targetSize)));
    };
    const ro = new ResizeObserver(([e]) =>
      measure(e.contentRect.width, e.contentRect.height),
    );
    ro.observe(el);
    return () => ro.disconnect();
  }, [compareMode]);

  // Playback timer
  useEffect(() => {
    if (!active || !playing || n < 2 || compareMode !== "blink") return;
    const t = window.setInterval(() => onIndex((index + 1) % n), 1000 / speed);
    return () => window.clearInterval(t);
  }, [active, playing, speed, index, n, onIndex, compareMode]);

  // Fullscreen event listener
  useEffect(() => {
    const onFsChange = () => setIsFullscreen(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", onFsChange);
    return () => document.removeEventListener("fullscreenchange", onFsChange);
  }, []);

  // Keyboard navigation
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (
        e.defaultPrevented ||
        e.metaKey ||
        e.ctrlKey ||
        e.altKey ||
        document.querySelector("[aria-modal=true]") ||
        !canvasRef.current?.offsetParent
      )
        return;
      const t = e.target instanceof Element ? e.target : null;
      if (
        t?.closest(
          'input, select, textarea, [contenteditable="true"], [role=tab], [role=radio]',
        )
      )
        return;

      if (e.key === " ") {
        if (t?.closest('button, a, summary, [role="tab"], [role="radio"]'))
          return;
        e.preventDefault();
        setPlaying((p) => !p);
      } else if (e.key === "ArrowRight") {
        setPlaying(false);
        onIndex((index + 1) % Math.max(n, 1));
      } else if (e.key === "ArrowLeft") {
        setPlaying(false);
        onIndex((index - 1 + n) % Math.max(n, 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        onCycleBand?.(1);
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        onCycleBand?.(-1);
      } else if (e.key === "i" || e.key === "I") {
        onToggleInvert?.();
      } else if (e.key === "d" || e.key === "D") {
        onCycleMode?.();
      } else if (e.key === "f" || e.key === "F") {
        if (!document.fullscreenElement) {
          playerContainerRef.current?.requestFullscreen?.();
        } else {
          document.exitFullscreen?.();
        }
      } else if (e.key === "?") {
        onOpenShortcuts?.();
      } else if (e.key === "+" || e.key === "=") {
        setSpeed(
          (s) => SPEEDS[Math.min(SPEEDS.indexOf(s) + 1, SPEEDS.length - 1)],
        );
      } else if (e.key === "-") {
        setSpeed((s) => SPEEDS[Math.max(SPEEDS.indexOf(s) - 1, 0)]);
      } else if (e.key === "Escape") {
        setRulerPoints({ start: null, current: null });
        setRulerActive(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [
    index,
    n,
    onIndex,
    onToggleInvert,
    onCycleBand,
    onCycleMode,
    onOpenShortcuts,
  ]);

  // Transform image pixels once, then compose images and annotations in the same coordinate space.
  const renderFrameToCanvas = useCallback(
    (
      c: HTMLCanvasElement,
      targetItem: BlinkItem | null | undefined,
      cacheKeyRef: { current: string | null },
      ovImg?: HTMLImageElement,
      crosshair?: { x: number; y: number } | null,
    ) => {
      const dpr = window.devicePixelRatio || 1;
      c.width = Math.round(size * dpr);
      c.height = Math.round(size * dpr);
      const ctx = c.getContext("2d")!;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = "#070a14";
      ctx.fillRect(0, 0, size, size);
      const drawImage = (
        it: BlinkItem | null | undefined,
        overlay?: HTMLImageElement,
      ) => {
        const img = it && images.get(it.url);
        if (!img) return;
        const scratch = document.createElement("canvas");
        scratch.width = img.naturalWidth;
        scratch.height = img.naturalHeight;
        const sc = scratch.getContext("2d", { willReadFrequently: true })!;
        sc.drawImage(img, 0, 0);
        // Difference and RGB images encode scientific colours. Keep their palettes intact.
        if (it?.kind !== "diff" && it?.kind !== "rgb")
          applyColormapToCanvas(
            sc,
            scratch.width,
            scratch.height,
            colormap,
            minClip,
            maxClip,
          );
        ctx.save();
        ctx.translate(pan.x, pan.y);
        ctx.scale(zoom, zoom);
        ctx.imageSmoothingEnabled = false;
        ctx.drawImage(scratch, 0, 0, size, size);
        if (overlay) ctx.drawImage(overlay, 0, 0, size, size);
        ctx.restore();
        cacheKeyRef.current = it?.url ?? null;
      };
      if (compareMode === "wipe" && secondaryItem) {
        ctx.save();
        ctx.beginPath();
        ctx.rect(0, 0, size * wipeRatio, size);
        ctx.clip();
        drawImage(targetItem, ovImg);
        ctx.restore();
        ctx.save();
        ctx.beginPath();
        ctx.rect(size * wipeRatio, 0, size * (1 - wipeRatio), size);
        ctx.clip();
        drawImage(secondaryItem, secondaryOverlayImg);
        ctx.restore();
      } else drawImage(targetItem, ovImg);
      const px = size / gridN,
        cx = size / 2;
      ctx.save();
      ctx.translate(pan.x, pan.y);
      ctx.scale(zoom, zoom);
      ctx.strokeStyle = "#38bdf8";
      ctx.lineWidth = 1.5 / zoom;
      for (const [dx, dy] of [
        [1, 0],
        [-1, 0],
        [0, 1],
        [0, -1],
      ]) {
        ctx.beginPath();
        ctx.moveTo(cx + dx * 10, cx + dy * 10);
        ctx.lineTo(cx + dx * 22, cx + dy * 22);
        ctx.stroke();
      }
      if (crosshair) {
        const x = (crosshair.x - pan.x) / zoom,
          y = (crosshair.y - pan.y) / zoom;
        ctx.save();
        ctx.setLineDash([4 / zoom, 4 / zoom]);
        ctx.strokeStyle = "#e8ff00";
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, size);
        ctx.moveTo(0, y);
        ctx.lineTo(size, y);
        ctx.stroke();
        ctx.restore();
      }
      if (pm && targetItem) {
        const off = pmOffsetArcsec(pm, targetItem.mjd);
        ctx.beginPath();
        ctx.arc(
          cx - (off.east / pixscaleArcsec) * px,
          cx - (off.north / pixscaleArcsec) * px,
          Math.max(8, px * 1.8),
          0,
          2 * Math.PI,
        );
        ctx.stroke();
      }
      // These marks describe the active epoch; do not reuse them on the second epoch.
      if (targetItem === item && compareMode !== "wipe") {
        if (gaia?.length)
          drawGaia(
            ctx,
            gaia,
            px,
            gridN,
            size,
            pixscaleArcsec,
            colormap === "inverted",
          );
        if (catwise?.length)
          drawCatwise(
            ctx,
            catwise,
            px,
            gridN,
            size,
            pixscaleArcsec,
            colormap === "inverted",
          );
        for (const m of wise ?? []) {
          ctx.strokeStyle = "#fbbf24";
          const x = (m.x + 0.5) * px,
            y = (gridN - 0.5 - m.y) * px;
          ctx.strokeRect(x - 4, y - 4, 8, 8);
        }
        if (markers?.length) drawMarkers(ctx, markers, px, gridN);
        if (selected)
          drawSelection(ctx, selected, px, gridN, colormap === "inverted");
      }
      ctx.restore();
      ctx.fillStyle = "#38bdf8";
      ctx.font = "11px ui-monospace, monospace";
      const arcsec = Math.min(
        60,
        ((size - 24) * gridN * pixscaleArcsec) / (size * zoom),
      );
      ctx.fillRect(12, size - 16, (arcsec / pixscaleArcsec) * px * zoom, 2);
      ctx.fillText(`${arcsec.toFixed(0)} arcsec`, 12, size - 22);
      ctx.fillText("N up / E left", size - 105, 20);
      if (rulerPoints.start && rulerPoints.current) {
        const a = rulerPoints.start,
          b = rulerPoints.current;
        ctx.strokeStyle = "#e8ff00";
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
        const distance =
          (Math.hypot(a.x - b.x, a.y - b.y) / zoom / size) *
          gridN *
          pixscaleArcsec;
        ctx.fillStyle = "#e8ff00";
        ctx.fillText(
          `${distance.toFixed(2)} arcsec`,
          Math.min(a.x, b.x),
          Math.max(15, Math.min(a.y, b.y) - 6),
        );
      }
      if (zoom > 1.05) {
        const mm = 64,
          x = size - mm - 10,
          y = size - mm - 10;
        ctx.fillStyle = "#070a14";
        ctx.fillRect(x, y, mm, mm);
        ctx.strokeStyle = "#3b4d79";
        ctx.strokeRect(x, y, mm, mm);
        ctx.strokeStyle = "#e8ff00";
        ctx.strokeRect(
          x + Math.max(0, (-pan.x / (size * zoom)) * mm),
          y + Math.max(0, (-pan.y / (size * zoom)) * mm),
          mm / zoom,
          mm / zoom,
        );
      }
      if (compareMode === "wipe") {
        ctx.strokeStyle = "#e8ff00";
        ctx.beginPath();
        ctx.moveTo(size * wipeRatio, 0);
        ctx.lineTo(size * wipeRatio, size);
        ctx.stroke();
      }
    },
    [
      size,
      gridN,
      images,
      colormap,
      minClip,
      maxClip,
      pan,
      zoom,
      compareMode,
      wipeRatio,
      secondaryItem,
      secondaryOverlayImg,
      pm,
      pixscaleArcsec,
      item,
      gaia,
      catwise,
      wise,
      markers,
      selected,
      rulerPoints,
    ],
  );

  useEffect(() => {
    const image = item && images.get(item.url);
    if (!image) return;
    const c = document.createElement("canvas");
    c.width = image.naturalWidth;
    c.height = image.naturalHeight;
    const ctx = c.getContext("2d")!;
    ctx.drawImage(image, 0, 0);
    setHistogram(computeImageHistogram(ctx, c.width, c.height, 32));
  }, [item, images, imagesVersion]);

  // Draw primary canvas
  useEffect(() => {
    const c = canvasRef.current;
    if (!c) return;
    renderFrameToCanvas(c, item, lastDrawn, overlayImg, hoverCoord);
  }, [
    item,
    images,
    imagesVersion,
    size,
    gridN,
    pixscaleArcsec,
    colormap,
    minClip,
    maxClip,
    pm,
    markers,
    gaia,
    overlayImg,
    overlaysVersion,
    hoverCoord,
    renderFrameToCanvas,
  ]);

  // Draw secondary canvas (side-by-side mode)
  useEffect(() => {
    const c2 = secondCanvasRef.current;
    if (!c2 || compareMode !== "side-by-side") return;
    renderFrameToCanvas(
      c2,
      secondaryItem,
      lastDrawnSecondary,
      secondaryOverlayImg,
      hoverCoord,
    );
  }, [
    compareMode,
    secondaryItem,
    images,
    imagesVersion,
    size,
    gridN,
    pixscaleArcsec,
    colormap,
    minClip,
    maxClip,
    pm,
    markers,
    gaia,
    secondaryOverlayImg,
    overlaysVersion,
    hoverCoord,
    renderFrameToCanvas,
  ]);

  const isFrameFailed = Boolean(item && failedImages?.has(item.url));
  const loading = Boolean(item && !images.get(item.url) && !isFrameFailed);
  const isSecondaryFailed = Boolean(
    secondaryItem && failedImages?.has(secondaryItem.url),
  );
  const secondaryLoading = Boolean(
    secondaryItem && !images.get(secondaryItem.url) && !isSecondaryFailed,
  );
  const step = (d: number) => {
    setPlaying(false);
    onIndex((index + d + n) % n);
  };

  // Mouse & Pan/Zoom event handlers
  const handleMouseDown = (e: MouseEvent<HTMLCanvasElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = ((e.clientX - rect.left) * size) / rect.width;
    const y = ((e.clientY - rect.top) * size) / rect.height;

    if (rulerActive) {
      setRulerPoints({ start: { x, y }, current: { x, y } });
      return;
    }

    if (compareMode === "wipe") {
      setIsDraggingWipe(true);
      setWipeRatio(Math.max(0.02, Math.min(0.98, x / size)));
      return;
    }

    setIsDragging(true);
    setDragStart({ x: e.clientX, y: e.clientY });
  };

  const handleMouseMove = (e: MouseEvent<HTMLCanvasElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = ((e.clientX - rect.left) * size) / rect.width;
    const y = ((e.clientY - rect.top) * size) / rect.height;
    setHoverCoord({ x, y });

    if (rulerActive && rulerPoints.start && e.buttons === 1) {
      setRulerPoints((rp) => ({ ...rp, current: { x, y } }));
      return;
    }

    if (isDraggingWipe) {
      setWipeRatio(Math.max(0.02, Math.min(0.98, x / size)));
      return;
    }

    if (isDragging) {
      const dx = e.clientX - dragStart.x;
      const dy = e.clientY - dragStart.y;
      setPan((p) => {
        const maxPan = (size * (zoom - 1)) / 2;
        return {
          x: Math.max(-maxPan * 2, Math.min(maxPan * 2, p.x + dx)),
          y: Math.max(-maxPan * 2, Math.min(maxPan * 2, p.y + dy)),
        };
      });
      setDragStart({ x: e.clientX, y: e.clientY });
    }
  };

  const handleMouseUp = () => {
    setIsDragging(false);
    setIsDraggingWipe(false);
  };

  useEffect(() => {
    const nodes = [canvasRef.current, secondCanvasRef.current].filter(
      Boolean,
    ) as HTMLCanvasElement[];
    const wheel = (e: globalThis.WheelEvent) => {
      e.preventDefault();
      setZoom((z) => {
        const next = Math.min(8, Math.max(1, z * (e.deltaY < 0 ? 1.25 : 0.8)));
        if (next === 1) setPan({ x: 0, y: 0 });
        return next;
      });
    };
    for (const node of nodes)
      node.addEventListener("wheel", wheel, { passive: false });
    return () => {
      for (const node of nodes) node.removeEventListener("wheel", wheel);
    };
  }, [compareMode]);

  const handleDoubleClick = () => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
    setRulerPoints({ start: null, current: null });
  };

  // Touch support (pinch to zoom)
  const handleTouchStart = (e: TouchEvent<HTMLCanvasElement>) => {
    if (e.touches.length === 2) {
      const dist = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY,
      );
      setTouchDistance(dist);
    }
  };

  const handleTouchMove = (e: TouchEvent<HTMLCanvasElement>) => {
    if (e.touches.length === 2 && touchDistance) {
      const dist = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY,
      );
      const factor = dist / touchDistance;
      setZoom((z) => Math.min(8, Math.max(1, z * factor)));
      setTouchDistance(dist);
    }
  };

  const handleTouchEnd = () => setTouchDistance(null);

  // Probe calculation: computes grid pixel and celestial RA/Dec coordinates
  const probeInfo = useMemo(() => {
    if (!hoverCoord || !targetCenter) return null;
    const vx = (hoverCoord.x - pan.x) / zoom;
    const vy = (hoverCoord.y - pan.y) / zoom;
    if (vx < 0 || vx > size || vy < 0 || vy > size) return null;

    const pxFracX = vx / size;
    const pxFracY = vy / size;
    const gridX = Math.min(gridN - 1, Math.floor(pxFracX * gridN));
    const gridY = Math.min(gridN - 1, Math.floor((1 - pxFracY) * gridN));

    const { ra, dec } = gridPixToRaDec(
      gridX,
      gridY,
      gridN,
      pixscaleArcsec,
      targetCenter.ra,
      targetCenter.dec,
    );

    let val: number | null = null;
    const c = canvasRef.current;
    if (c) {
      try {
        const dpr = window.devicePixelRatio || 1;
        const ctx = c.getContext("2d");
        if (ctx) {
          const pixel = ctx.getImageData(
            Math.floor(hoverCoord.x * dpr),
            Math.floor(hoverCoord.y * dpr),
            1,
            1,
          ).data;
          val = Math.round(
            0.299 * pixel[0] + 0.587 * pixel[1] + 0.114 * pixel[2],
          );
        }
      } catch {
        // Tainted canvas or out of bounds fallback
      }
    }

    return { gridX, gridY, ra, dec, val };
  }, [hoverCoord, targetCenter, pan, zoom, size, gridN, pixscaleArcsec]);

  // Ruler measurement calculation
  const rulerDistance = useMemo(() => {
    if (!rulerPoints.start || !rulerPoints.current) return null;
    const dx = (rulerPoints.current.x - rulerPoints.start.x) / zoom;
    const dy = (rulerPoints.current.y - rulerPoints.start.y) / zoom;
    const px = Math.hypot(dx, dy);
    const gridDist = (px / size) * gridN;
    const arcsec = gridDist * pixscaleArcsec;
    return { px: gridDist, arcsec };
  }, [rulerPoints, zoom, size, gridN, pixscaleArcsec]);

  // Display auto-contrast auto-stretch
  const applyDisplayAuto = () => {
    if (!histogram.length) {
      setMinClip(15);
      setMaxClip(240);
      return;
    }
    let accum = 0;
    const total = histogram.reduce((a, b) => a + b, 0);
    let pMin = 0;
    let pMax = 255;
    const stepSize = 256 / histogram.length;

    for (let i = 0; i < histogram.length; i++) {
      accum += histogram[i];
      if (accum >= total * 0.05 && pMin === 0) pMin = Math.round(i * stepSize);
      if (accum >= total * 0.98) {
        pMax = Math.round(i * stepSize);
        break;
      }
    }
    setMinClip(Math.max(0, pMin));
    setMaxClip(Math.min(255, pMax));
  };

  const frameLabel = (it: BlinkItem, prefix = "") => (
    <div className="frame-label">
      <strong>
        {prefix}
        {it.title}
      </strong>
      <div className="meta tabular">
        {it.detector > 0 && (
          <span style={{ color: detectorColor(it.detector) }}>D{it.detector} </span>
        )}
        {[it.wavelengthUm ? `${it.wavelengthUm.toFixed(2)} µm` : "", it.note ?? ""]
          .filter(Boolean)
          .join(" · ")}
      </div>
      {it.warning && (
        <div
          className="meta"
          style={{ color: it.warning.level === "bad" ? "var(--error)" : "var(--variable)" }}
        >
          Flag: {it.warning.text}
        </div>
      )}
    </div>
  );

  return (
    <div
      ref={playerContainerRef}
      className={`player ${isFullscreen ? "is-fullscreen" : ""}`}
    >
      <div
        ref={wrapRef}
        className={`player-canvas ${compareMode === "side-by-side" ? "is-pair" : ""}`}
        data-tour="image"
      >
        <div className="flex items-center justify-center gap-3">
          <div className="player-frame">
            <canvas
              ref={canvasRef}
              onPointerDown={(e) => {
                pickDown.current = e.isPrimary
                  ? { x: e.clientX, y: e.clientY }
                  : null;
              }}
              onPointerUp={(e) => {
                const down = pickDown.current;
                pickDown.current = null;
                if (
                  !down ||
                  !onPick ||
                  rulerActive ||
                  compareMode === "wipe" ||
                  Math.hypot(e.clientX - down.x, e.clientY - down.y) >= 6
                )
                  return;
                const r = e.currentTarget.getBoundingClientRect();
                const x =
                  ((((e.clientX - r.left) * size) / r.width - pan.x) /
                    zoom /
                    size) *
                    gridN -
                  0.5;
                const y =
                  gridN -
                  0.5 -
                  ((((e.clientY - r.top) * size) / r.height - pan.y) /
                    zoom /
                    size) *
                    gridN;
                if (
                  x >= -0.5 &&
                  y >= -0.5 &&
                  x < gridN - 0.5 &&
                  y < gridN - 0.5
                )
                  onPick(x, y);
              }}
              style={{ width: size, height: size }}
              className={`pixelated block bg-black ${
                rulerActive
                  ? "cursor-crosshair"
                  : isDragging
                    ? "cursor-grabbing"
                    : zoom > 1
                      ? "cursor-grab"
                      : "cursor-crosshair"
              }`}
              role="img"
              aria-label={
                item ? `SPHEREx image: ${item.title}` : "SPHEREx observation"
              }
              onMouseDown={handleMouseDown}
              onMouseMove={handleMouseMove}
              onMouseUp={handleMouseUp}
              onMouseLeave={() => {
                setHoverCoord(null);
                setIsDragging(false);
              }}
              onDoubleClick={handleDoubleClick}
              onTouchStart={handleTouchStart}
              onTouchMove={handleTouchMove}
              onTouchEnd={handleTouchEnd}
            />

            {item && frameLabel(item, compareMode === "side-by-side" ? "A · " : "")}

            {(probeInfo || rulerDistance) && (
              <div className="frame-probe tabular">
                {probeInfo && (
                  <>
                    x <b>{probeInfo.gridX}</b> y <b>{probeInfo.gridY}</b> · RA{" "}
                    <b>{probeInfo.ra.toFixed(4)}°</b> Dec{" "}
                    <b>
                      {probeInfo.dec >= 0 ? "+" : "−"}
                      {Math.abs(probeInfo.dec).toFixed(4)}°
                    </b>
                    {probeInfo.val !== null && <> · display {probeInfo.val}</>}
                  </>
                )}
                {rulerDistance && (
                  <span style={{ color: "var(--variable)" }}>
                    {probeInfo ? " · " : ""}ruler {rulerDistance.px.toFixed(1)} px ={" "}
                    {rulerDistance.arcsec.toFixed(1)}″
                  </span>
                )}
              </div>
            )}

            {selectionCard && (
              <div className="frame-selection">{selectionCard}</div>
            )}
            {loading && (
              <div className="frame-state pointer-events-none" aria-hidden="true">
                <div className="stage-skeleton" style={{ width: "40%" }} />
                <span className="help">Loading image…</span>
              </div>
            )}
            {isFrameFailed && item && (
              <div className="frame-state">
                <p className="font-semibold">This image could not be loaded.</p>
                <p className="help">Retry to request it again.</p>
                {onRetryImage && (
                  <button
                    type="button"
                    onClick={() => onRetryImage(item.url)}
                    className="btn btn-sm"
                  >
                    Retry image
                  </button>
                )}
              </div>
            )}
          </div>

          {compareMode === "side-by-side" && secondaryItem && (
            <div className="player-frame">
              <canvas
                ref={secondCanvasRef}
                style={{ width: size, height: size }}
                className="pixelated block bg-black cursor-crosshair"
                role="img"
                aria-label={`Comparison image: ${secondaryItem.title}`}
                onMouseDown={handleMouseDown}
                onMouseMove={handleMouseMove}
                onMouseUp={handleMouseUp}
                onMouseLeave={() => {
                  setHoverCoord(null);
                  setIsDragging(false);
                }}
                onDoubleClick={handleDoubleClick}
              />
              {frameLabel(secondaryItem, "B · ")}
              {secondaryLoading && (
                <div className="frame-state pointer-events-none" aria-hidden="true">
                  <span className="help">Loading image…</span>
                </div>
              )}
              {isSecondaryFailed && (
                <div className="frame-state">
                  <p className="font-semibold">This image could not be loaded.</p>
                  {onRetryImage && (
                    <button
                      type="button"
                      onClick={() => onRetryImage(secondaryItem.url)}
                      className="btn btn-sm"
                    >
                      Retry
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {compareMode === "wipe" && (
        <div className="wipe-bar">
          <span>Wipe</span>
          <input
            type="range"
            min={0.02}
            max={0.98}
            step={0.01}
            value={wipeRatio}
            onChange={(e) => setWipeRatio(parseFloat(e.target.value))}
            aria-label="Split wipe position"
          />
          <span className="tabular w-10 text-right">
            {Math.round(wipeRatio * 100)}%
          </span>
        </div>
      )}

      <div className="transport" data-tour="player">
        <button
          type="button"
          onClick={() => step(-1)}
          className="btn btn-ghost btn-sm btn-icon"
          aria-label="Previous image"
          title="Previous image (←)"
          disabled={n < 2}
        >
          <IconPrev />
        </button>
        <button
          type="button"
          onClick={() => {
            if (compareMode !== "blink") setCompareMode("blink");
            setPlaying((p) => !p);
          }}
          className="btn btn-sm btn-icon"
          aria-label={playing && compareMode === "blink" ? "Pause" : "Play"}
          aria-keyshortcuts="Space"
          title="Play or pause (Space)"
          disabled={n < 2}
        >
          {playing && compareMode === "blink" ? <IconPause /> : <IconPlay />}
        </button>
        <button
          type="button"
          onClick={() => step(1)}
          className="btn btn-ghost btn-sm btn-icon"
          aria-label="Next image"
          title="Next image (→)"
          disabled={n < 2}
        >
          <IconNext />
        </button>
        <span className="count">
          {n ? index + 1 : 0} / {n}
        </span>
        <input
          aria-label="Image in sequence"
          aria-valuetext={item?.title}
          type="range"
          min={0}
          max={Math.max(n - 1, 0)}
          value={index}
          onChange={(e) => {
            setPlaying(false);
            onIndex(Number(e.target.value));
          }}
        />
        <select
          value={speed}
          onChange={(e) => setSpeed(Number(e.target.value))}
          className="input"
          aria-label="Blink rate"
          title="Blink rate in images per second (+ and − keys)"
        >
          {SPEEDS.map((s) => (
            <option key={s} value={s}>
              {s}/s
            </option>
          ))}
        </select>
        <div className="seg" role="group" aria-label="Comparison">
          <button
            type="button"
            aria-pressed={compareMode === "blink"}
            onClick={() => {
              setCompareMode("blink");
              setPlaying(true);
            }}
            title="Blink through the sequence"
          >
            Blink
          </button>
          <button
            type="button"
            aria-pressed={compareMode === "side-by-side"}
            onClick={() => {
              setCompareMode("side-by-side");
              setPlaying(false);
            }}
            title="This image and the next, side by side"
          >
            Pair
          </button>
          <button
            type="button"
            aria-pressed={compareMode === "wipe"}
            onClick={() => {
              setCompareMode("wipe");
              setPlaying(false);
            }}
            title="Drag a split line between this image and the next"
          >
            Wipe
          </button>
        </div>
        <div className="zoom" role="group" aria-label="Zoom">
          <button
            type="button"
            onClick={() => {
              setZoom((z) => {
                const nz = Math.max(1, Number((z / 1.3).toFixed(2)));
                if (nz === 1) setPan({ x: 0, y: 0 });
                return nz;
              });
            }}
            aria-label="Zoom out"
            title="Zoom out"
          >
            −
          </button>
          <span className="tabular">{zoom.toFixed(1)}×</span>
          <button
            type="button"
            onClick={() =>
              setZoom((z) => Math.min(8, Number((z * 1.3).toFixed(2))))
            }
            aria-label="Zoom in"
            title="Zoom in (or scroll on the image; double-click resets)"
          >
            +
          </button>
        </div>
        <button
          type="button"
          onClick={() => {
            setRulerActive(!rulerActive);
            if (rulerActive) setRulerPoints({ start: null, current: null });
          }}
          className={`btn btn-sm btn-icon ruler ${rulerActive ? "" : "btn-ghost"}`}
          style={rulerActive ? { color: "var(--variable)", borderColor: "var(--variable)" } : undefined}
          aria-label="Ruler"
          title="Measure a separation: drag across the image (Esc clears)"
          aria-pressed={rulerActive}
        >
          <IconRuler />
        </button>
        <Popover
          label="Palette and contrast"
          button={<IconPalette />}
          buttonClass="btn btn-ghost btn-sm btn-icon"
          placement="up"
          width="w-[min(300px,calc(100vw-24px))]"
        >
          <div className="flex flex-col gap-3 text-xs">
            <div>
              <p className="field-label mb-1.5">Palette (exposures and stacks)</p>
              <div className="flex flex-wrap gap-1">
                {COLORMAP_OPTIONS.map((c) => (
                  <button
                    key={c.id}
                    type="button"
                    className="tog"
                    aria-pressed={colormap === c.id}
                    onClick={() => {
                      setSelectedColormap(c.id);
                      if (c.id === "inverted" && !invert) onToggleInvert?.();
                      else if (c.id !== "inverted" && invert) onToggleInvert?.();
                    }}
                    title={c.description}
                  >
                    {c.label}
                  </button>
                ))}
              </div>
            </div>
            <div className="flex gap-2">
              <button type="button" onClick={applyDisplayAuto} className="btn btn-sm" title="Stretch the displayed image to its histogram">
                Auto contrast
              </button>
              <button
                type="button"
                onClick={() => {
                  setMinClip(0);
                  setMaxClip(255);
                }}
                className="btn btn-ghost btn-sm"
              >
                Reset
              </button>
            </div>
            <div className="border-t border-line pt-2 text-ink-300">
              <p className="field-label mb-1">Key</p>
              <p>Cross: field centre. Ring: position predicted from the catalogue proper motion.</p>
              {gaia && gaia.length > 0 && <p>Circles: Gaia DR3 stars.</p>}
              {!!catwise?.length && <p>Diamonds: CatWISE sources.</p>}
              {!!wise?.length && <p>Squares: AllWISE sources.</p>}
              <p className="mt-1 text-ink-500">Hover the image for coordinates; display values are not physical flux.</p>
            </div>
          </div>
        </Popover>
        <button
          type="button"
          onClick={() => {
            if (!isFullscreen)
              playerContainerRef.current?.requestFullscreen?.();
            else document.exitFullscreen?.();
          }}
          className="btn btn-ghost btn-sm btn-icon fullscreen"
          aria-label={isFullscreen ? "Exit full screen" : "Full screen"}
          title="Full screen (F)"
        >
          <IconFullscreen />
        </button>
      </div>
    </div>
  );
}
