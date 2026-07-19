/** SSE stream client: reconnect + composite-cursor resume, signals-based.
 *
 * Fixtures mode (VITE_FIXTURES=1) replays fixtures/dashboard/stream.jsonl on a
 * timer with no backend — the golden contract fixtures ARE the dev data.
 */

import { computed, signal } from "@preact/signals";
import { api, ApiRequestError } from "./api";
import { applySpectrumPayload } from "./spectrum";
import { cursorAfterEnvelope } from "./stream_cursor";
import type { Envelope, HepPayload, LogPayload } from "./types";

export type ConnState = "connecting" | "live" | "retrying" | "fixtures" | "auth";

const MAX_EVENTS = 2000;

export const connState = signal<ConnState>("connecting");
export const events = signal<Envelope[]>([]);
export const paused = signal(false);
/** Events buffered while paused, flushed on resume. */
const pausedBuffer: Envelope[] = [];
export const pausedCount = signal(0);
export const lastCursor = signal<string | null>(null);
export const serverInfo = signal<{ server: string; version: string } | null>(null);

export const eventCount = computed(() => events.value.length);

type Listener = (e: Envelope) => void;
const listeners = new Set<Listener>();

/** Subscribe to live envelopes (notifications, dictation progress, …). */
export function onEnvelope(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function ingest(envelope: Envelope): void {
  lastCursor.value = cursorAfterEnvelope(lastCursor.value, envelope);
  if (envelope.type === "hello") {
    const p = envelope.payload as { server?: string; version?: string };
    if (p.server) serverInfo.value = { server: p.server, version: p.version ?? "" };
    return;
  }
  // Live mic spectrum (B087): high-rate, coalesced — never flood the timeline
  if (envelope.source === "serve") {
    const p = envelope.payload as { kind?: string };
    if (p.kind === "serve.spectrum") {
      applySpectrumPayload(envelope.payload as { kind?: string });
      for (const fn of listeners) fn(envelope);
      return;
    }
  }
  for (const fn of listeners) fn(envelope);
  if (paused.value) {
    pausedBuffer.push(envelope);
    if (pausedBuffer.length > MAX_EVENTS) pausedBuffer.shift();
    pausedCount.value = pausedBuffer.length;
    return;
  }
  push(envelope);
}

function push(envelope: Envelope): void {
  const next = events.value.length >= MAX_EVENTS ? events.value.slice(1) : events.value.slice();
  next.push(envelope);
  events.value = next;
}

export function setPaused(value: boolean): void {
  paused.value = value;
  if (!value && pausedBuffer.length) {
    const merged = [...events.value, ...pausedBuffer];
    events.value = merged.slice(-MAX_EVENTS);
    pausedBuffer.length = 0;
    pausedCount.value = 0;
  }
}

// ---------------------------------------------------------------------------

let source: EventSource | null = null;
let retryTimer: number | undefined;

export function connect(): void {
  if (import.meta.env.VITE_FIXTURES) {
    void connectFixtures();
    return;
  }
  void connectLive();
}

async function connectLive(): Promise<void> {
  disconnect();
  connState.value = "connecting";
  if (!lastCursor.value) {
    // first connect: load recent history so the feed is never empty, then
    // stream from exactly that cursor (gapless handoff)
    try {
      const page = await api.events({ limit: 300 });
      for (const e of page.events) push(e);
      lastCursor.value = page.cursor;
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 401) {
        connState.value = "auth";
        return;
      }
      // history is best-effort; the live stream can still work
    }
  }
  const since = lastCursor.value ? `?since=${encodeURIComponent(lastCursor.value)}` : "";
  source = new EventSource(`/api/v1/stream${since}`);
  source.onopen = () => {
    connState.value = "live";
  };
  source.onmessage = (msg) => {
    try {
      ingest(JSON.parse(msg.data) as Envelope);
    } catch {
      // tolerate malformed lines; the stream must survive
    }
  };
  source.onerror = () => {
    // EventSource auto-reconnects, but a 401 loops forever: probe once
    connState.value = "retrying";
    void fetch("/api/v1/health", { credentials: "same-origin" }).then((res) => {
      if (res.status === 401) {
        disconnect();
        connState.value = "auth";
      }
    });
  };
}

export function disconnect(): void {
  source?.close();
  source = null;
  if (retryTimer) window.clearTimeout(retryTimer);
}

// ------------------------------------------------------------------ fixtures

async function connectFixtures(): Promise<void> {
  connState.value = "fixtures";
  const raw = (await import("../../../fixtures/dashboard/stream.jsonl?raw")).default as string;
  const lines = raw
    .split("\n")
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l) as Envelope);
  let i = 0;
  const emit = () => {
    ingest(lines[i % lines.length]);
    i += 1;
    window.setTimeout(emit, i < lines.length ? 350 : 1800 + Math.random() * 2500);
  };
  emit();
}

// ---------------------------------------------------------------- semantics

export type Severity = "error" | "warn" | "ok" | "info" | "ambient" | "muted";

/** Semantic severity for an envelope — drives card accents + filters. */
export function severityOf(e: Envelope): Severity {
  const p = e.payload as HepPayload & LogPayload & { type?: string; status?: string; ok?: boolean };
  if (e.source === "system") {
    return p.level === "error" ? "error" : p.level === "warn" ? "warn" : "muted";
  }
  if (e.source === "usage") return p.ok === false ? "error" : "muted";
  if (e.source === "delivery") {
    if (p.type === "outcome") {
      return p.status === "delivered" ? "ok" : p.status === "uncertain" ? "warn" : "error";
    }
    return "warn";
  }
  const kind = p.kind ?? "";
  if (kind.endsWith(".error") || kind === "answer.rejected") return "error";
  if (
    kind === "agent.blocked" ||
    kind === "agent.needs_input" ||
    kind === "agent.question_changed" ||
    kind === "answer.confirmation_required" ||
    kind === "answer.delivery_uncertain" ||
    kind === "ambient.wake_near_miss" ||
    kind === "bridge.degraded"
  )
    return "warn";
  if (kind === "agent.completed" || kind === "answer.delivered" || kind === "bridge.recovered")
    return "ok";
  if (kind.startsWith("ambient.")) return "ambient";
  if (kind.startsWith("watch.") || kind.startsWith("serve.")) return "muted";
  return "info";
}

export function kindOf(e: Envelope): string {
  const p = e.payload as Record<string, unknown>;
  if (e.source === "system") return String(p.event ?? "log");
  if (e.source === "usage") return `usage.${p.kind}`;
  if (e.source === "delivery") return p.type === "outcome" ? `delivery.${p.status}` : "delivery.bound";
  return String(p.kind ?? "event");
}

export function timeOf(e: Envelope): Date | null {
  const p = e.payload as Record<string, unknown>;
  if (typeof p.observed_at === "string") return new Date(p.observed_at);
  const ts = p.ts ?? p.created_at;
  if (typeof ts === "number") return new Date(ts * 1000);
  return null;
}
