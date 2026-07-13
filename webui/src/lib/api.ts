/** Typed fetch client for hark.dashboard.v1 REST endpoints. */

import type {
  AnswerResponse,
  ConfigResponse,
  ContextResponse,
  DeliveriesResponse,
  EventsPage,
  HealthResponse,
  SessionsResponse,
  UsageResponse,
} from "./types";

export class ApiRequestError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok && body?.error) {
    throw new ApiRequestError(res.status, body.error.code ?? "error", body.error.message ?? "");
  }
  if (!res.ok) throw new ApiRequestError(res.status, "http_error", `HTTP ${res.status}`);
  return body as T;
}

export const api = {
  auth: (token: string) =>
    request<{ ok: true }>("/api/v1/auth", { method: "POST", body: JSON.stringify({ token }) }),
  health: () => request<HealthResponse>("/api/v1/health"),
  config: () => request<ConfigResponse>("/api/v1/config"),
  events: (params: { since?: string; sources?: string[]; limit?: number } = {}) => {
    const qs = new URLSearchParams();
    if (params.since) qs.set("since", params.since);
    if (params.sources?.length) qs.set("sources", params.sources.join(","));
    if (params.limit) qs.set("limit", String(params.limit));
    const q = qs.toString();
    return request<EventsPage>(`/api/v1/events${q ? `?${q}` : ""}`);
  },
  sessions: () => request<SessionsResponse>("/api/v1/herdr/sessions"),
  context: (session: string, pane: string, lines = 60) =>
    request<ContextResponse>(
      `/api/v1/herdr/context/${encodeURIComponent(session)}/${encodeURIComponent(pane)}?lines=${lines}`,
    ),
  deliveries: () => request<DeliveriesResponse>("/api/v1/deliveries"),
  usage: () => request<UsageResponse>("/api/v1/usage"),
  answer: (event_id: string, opts: { text?: string; keys?: string[] }) =>
    request<AnswerResponse>("/api/v1/answer", {
      method: "POST",
      body: JSON.stringify({ event_id, text: opts.text ?? null, keys: opts.keys ?? null }),
    }),
  prompt: (text: string, session_id?: string | null) =>
    request<{ ok: true; event_id: string }>("/api/v1/prompt", {
      method: "POST",
      body: JSON.stringify({ text, session_id: session_id ?? null }),
    }),
  transcribe: async (blob: Blob): Promise<{ ok: true; text: string; provider?: string }> => {
    const res = await fetch("/api/v1/dictation/transcribe", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": blob.type || "application/octet-stream" },
      body: blob,
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new ApiRequestError(res.status, body?.error?.code ?? "error", body?.error?.message ?? "");
    }
    return body;
  },
  dictation: (action: "start" | "stop" | "cancel", body: Record<string, unknown> = {}) =>
    request<{ ok: boolean; state: string; text?: string | null }>(
      `/api/v1/dictation/${action}`,
      { method: "POST", body: JSON.stringify(body) },
    ),
};
