import type { Target } from "../lib/viewState";
import { ExamplesMenu } from "./ExamplesMenu";
import { SearchBar } from "./SearchBar";
import { BrandMark, IconCommand, IconHelp } from "./icons";

interface Props {
  showSearch: boolean;
  fov: number;
  onFovChange: (f: number) => void;
  onTarget: (t: Target) => void;
  onOpenExample: (s: string) => void;
  onStartTour: () => void;
  onHome: () => void;
  onOpenPalette: () => void;
  onOpenBatch: () => void;
}

const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

/** Slim top bar: brand, search (in the viewer), examples, batch, command palette, tour. */
export function Header(p: Props) {
  return (
    <header className={`topbar ${p.showSearch ? "has-search" : ""}`}>
      <a
        href="/"
        className="brand"
        onClick={(e) => {
          if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
          e.preventDefault();
          p.onHome();
        }}
        aria-label="GAMAS, the changing sky: home"
      >
        <BrandMark className="brand-mark" />
        <span className="brand-name">GAMAS</span>
        <span className="brand-tag">The changing sky</span>
      </a>
      {p.showSearch ? (
        <div className="topbar-search" data-tour="search">
          <SearchBar fov={p.fov} onFovChange={p.onFovChange} onTarget={p.onTarget} />
        </div>
      ) : (
        <span className="topbar-spacer" />
      )}
      {p.showSearch && <span className="topbar-spacer hide-sm" />}
      <nav className="topbar-actions" aria-label="Tools">
        <span className="hide-sm">
          <ExamplesMenu onOpen={p.onOpenExample} />
        </span>
        <button type="button" className="btn btn-ghost btn-sm hide-sm" onClick={p.onOpenBatch}>
          Batch
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-sm cmd-trigger"
          onClick={p.onOpenPalette}
          aria-label="Open command palette"
          aria-keyshortcuts={isMac ? "Meta+K /" : "Control+K /"}
          title="Commands"
        >
          <IconCommand />
          <span className="hide-sm">Commands</span>
          <span className="kbd hide-sm">{isMac ? "⌘K" : "Ctrl K"}</span>
        </button>
        <button type="button" className="btn btn-ghost btn-sm btn-icon" onClick={p.onStartTour} aria-label="Guided tour" title="Guided tour">
          <IconHelp />
        </button>
      </nav>
    </header>
  );
}
