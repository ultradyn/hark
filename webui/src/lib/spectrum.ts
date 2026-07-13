/** Live host-mic spectrum state (B087). Fed by serve.spectrum SSE frames. */

import { signal } from "@preact/signals";

const COLLAPSED_KEY = "hark.spectrum.collapsed";
/** User collapsed while recording — suppress auto-expand on future record periods. */
const SUPPRESS_AUTO_KEY = "hark.spectrum.suppressAutoExpand";

export interface SpectrumFrame {
  bands: number[];
  ts: number;
  recording: boolean;
  sample_rate?: number;
  max_hz?: number;
  source?: string;
}

export const spectrumFrame = signal<SpectrumFrame | null>(null);
/** Section collapsed (localStorage-backed preference). */
export const spectrumCollapsed = signal(readBool(COLLAPSED_KEY, false));
/** When true, recording does not force-expand the panel. */
export const suppressAutoExpand = signal(readBool(SUPPRESS_AUTO_KEY, false));

function readBool(key: string, fallback: boolean): boolean {
  try {
    const v = localStorage.getItem(key);
    if (v === null) return fallback;
    return v === "1";
  } catch {
    return fallback;
  }
}

function writeBool(key: string, value: boolean): void {
  try {
    localStorage.setItem(key, value ? "1" : "0");
  } catch {
    /* ignore */
  }
}

export function setSpectrumCollapsed(
  collapsed: boolean,
  opts: { recording?: boolean } = {},
): void {
  spectrumCollapsed.value = collapsed;
  writeBool(COLLAPSED_KEY, collapsed);
  if (collapsed && opts.recording) {
    // Operator deliberately hid the panel during rec — honor until they expand
    suppressAutoExpand.value = true;
    writeBool(SUPPRESS_AUTO_KEY, true);
  }
  if (!collapsed) {
    suppressAutoExpand.value = false;
    writeBool(SUPPRESS_AUTO_KEY, false);
  }
}

/** Apply a serve.spectrum payload (or clear on idle silence). */
export function applySpectrumPayload(p: {
  kind?: string;
  bands?: unknown;
  ts?: unknown;
  recording?: unknown;
  sample_rate?: unknown;
  max_hz?: unknown;
  source?: unknown;
}): void {
  if (p.kind !== "serve.spectrum") return;
  const bands = Array.isArray(p.bands)
    ? p.bands.map((b) => {
        const n = Number(b);
        return Number.isFinite(n) ? Math.max(0, Math.min(1, n)) : 0;
      })
    : [];
  const recording = Boolean(p.recording);
  const prevRec = spectrumFrame.value?.recording ?? false;
  spectrumFrame.value = {
    bands,
    ts: typeof p.ts === "number" ? p.ts : Date.now() / 1000,
    recording,
    sample_rate: typeof p.sample_rate === "number" ? p.sample_rate : undefined,
    max_hz: typeof p.max_hz === "number" ? p.max_hz : undefined,
    source: typeof p.source === "string" ? p.source : undefined,
  };

  // Auto-expand while recording unless user suppressed during a prior rec period
  if (recording && !suppressAutoExpand.value && spectrumCollapsed.value) {
    spectrumCollapsed.value = false;
    writeBool(COLLAPSED_KEY, false);
  }

  // Recording edge is only for consumers; frame already updated above
  void prevRec;
}
