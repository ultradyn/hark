/** Polled snapshot stores + live derived state (sessions, deliveries, health…).
 *
 * Views poll while mounted; watch/delivery stream events trigger immediate
 * refreshes so the UI feels live without tight poll loops.
 */

import { signal } from "@preact/signals";
import { useEffect } from "preact/hooks";
import { api } from "./api";
import { onEnvelope } from "./stream";
import type {
  DeliveriesResponse,
  HealthResponse,
  SessionsResponse,
  UsageResponse,
} from "./types";

export const sessions = signal<SessionsResponse | null>(null);
export const deliveries = signal<DeliveriesResponse | null>(null);
export const usage = signal<UsageResponse | null>(null);
export const health = signal<(HealthResponse & { pipeline?: PipelineState }) | null>(null);
/** true while a conference hold is active (derived from system events). */
export const conferenceHold = signal(false);
/** pane selected for drill-in (herdr view / command palette). */
export const selectedPane = signal<{ session_id: string; pane_id: string } | null>(null);

export interface PipelineState {
  busy_lock?: boolean;
  mic_lock?: boolean;
  ambient_pause?: boolean;
  announce_hold_queued?: number;
  harkd?: { running: boolean; pids: number[] };
  mode_a?: { running: boolean; pids: number[] };
  state_dir?: string;
  [k: string]: unknown;
}

type Fetcher = () => Promise<void>;

const fetchers: Record<string, Fetcher> = {
  sessions: async () => {
    sessions.value = await api.sessions();
  },
  deliveries: async () => {
    deliveries.value = await api.deliveries();
  },
  usage: async () => {
    usage.value = await api.usage();
  },
  health: async () => {
    health.value = (await api.health()) as never;
  },
};

const inflight = new Set<string>();

export async function refresh(name: keyof typeof fetchers): Promise<void> {
  if (inflight.has(name)) return;
  inflight.add(name);
  try {
    await fetchers[name]();
  } catch {
    // snapshots are best-effort; the stream badge reports connectivity
  } finally {
    inflight.delete(name);
  }
}

/** Poll a snapshot while the calling component is mounted. */
export function usePoll(name: keyof typeof fetchers, intervalMs: number): void {
  useEffect(() => {
    void refresh(name);
    const t = window.setInterval(() => void refresh(name), intervalMs);
    return () => window.clearInterval(t);
  }, [name, intervalMs]);
}

// event-driven refreshes + conference hold tracking (module init, once)
let wired = false;
export function wireDataRefreshes(): void {
  if (wired) return;
  wired = true;
  onEnvelope((e) => {
    if (e.source === "watch") {
      void refresh("sessions");
      void refresh("deliveries");
    } else if (e.source === "delivery") {
      void refresh("deliveries");
    } else if (e.source === "usage") {
      void refresh("usage");
    } else if (e.source === "system") {
      const ev = (e.payload as { event?: string }).event ?? "";
      if (ev === "announce.held") conferenceHold.value = true;
      else if (ev === "announce.resumed") conferenceHold.value = false;
    }
  });
}
