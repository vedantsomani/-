// Share actions used by the target strip and the command palette.
import { apiUrl, fingerprintQuery } from "./api";
import type { Target } from "./viewState";

/** The investigation report for exactly this view (self-contained HTML from the backend). */
export function reportUrl(target: Target, fov: number) {
  const q = new URLSearchParams(fingerprintQuery({ ra: target.ra, dec: target.dec, fov, pm: target.pm }));
  q.set("url", window.location.href);
  if (target.label) q.set("name", target.label);
  return apiUrl(`/api/investigation?${q}`);
}

export async function copyLink(): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(location.href);
    return true;
  } catch {
    return false;
  }
}
