/** TypeScript mirror of hark.dashboard.v1 (schemas/dashboard-v1/). */

export type Source = "watch" | "ambient" | "system" | "usage" | "delivery" | "serve";

export interface Envelope<P = Payload> {
  schema: "hark.dashboard.v1";
  type: "hello" | "event";
  source: Source | (string & {});
  cursor: string;
  payload: P;
}

/** HEP hark.event.v1 passthrough (open kinds; unknown fields flow through). */
export interface HepPayload {
  schema?: "hark.event.v1";
  kind: string;
  event_id?: string;
  observed_at?: string;
  session_id?: string;
  priority?: number;
  target?: {
    workspace_id?: string | null;
    tab_id?: string | null;
    pane_id?: string;
    terminal_id?: string | null;
    pane_revision?: number;
    agent?: string | null;
    friendly_name?: string | null;
  };
  state?: { from?: string; to?: string; blocked_epoch?: number };
  question?: {
    kind?: string;
    text?: string | null;
    choices?: string[] | null;
    fingerprint?: string | null;
    confidence?: number;
    risk?: string | null;
  };
  disposition?: string;
  // ambient
  text?: string | null;
  phrase?: string | null;
  stream_id?: string | null;
  partial?: boolean;
  final?: boolean;
  seq?: number;
  attempts?: unknown[];
  count?: number;
  error?: string | null;
  [k: string]: unknown;
}

export interface LogPayload {
  ts: number;
  seq?: number;
  level: "debug" | "info" | "warn" | "error";
  component: string;
  event: string;
  message: string;
  data?: Record<string, unknown>;
  pid?: number;
}

export interface UsagePayload {
  kind: "tts" | "stt";
  ts: number;
  provider?: string | null;
  voice?: string | null;
  ok: boolean;
  chars?: number;
  words?: number;
  audio_ms?: number;
  latency_ms?: number;
  error?: string | null;
  meta?: Record<string, unknown>;
}

export type DeliveryPayload =
  | ({ type: "bound" } & BoundEvent)
  | { type: "outcome"; event_id: string; status: string; ts: number; [k: string]: unknown };

export interface BoundEvent {
  event_id: string;
  session_id: string;
  pane_id: string;
  pane_revision?: number;
  question_fingerprint?: string | null;
  question_text?: string | null;
  risk?: string | null;
  status?: string;
  created_at?: number;
}

export interface ServePayload {
  kind: `serve.${string}`;
  [k: string]: unknown;
}

/** Live FFT bands from host mic capture (B087). Not stored in event history. */
export interface SpectrumPayload {
  kind: "serve.spectrum";
  bands: number[];
  ts: number;
  recording: boolean;
  sample_rate?: number;
  max_hz?: number;
  source?: string;
}

export type Payload = HepPayload | LogPayload | UsagePayload | DeliveryPayload | ServePayload;

// ---- snapshots ----

export interface UpdateInfo {
  current_version?: string;
  latest_version?: string | null;
  update_available?: boolean;
  html_url?: string | null;
  tag_name?: string | null;
  checked_at?: number | null;
  checked_at_iso?: string | null;
  from_cache?: boolean;
  stale?: boolean;
  disabled?: boolean;
  error?: string | null;
  repo?: string;
}

export interface HealthResponse {
  schema: string;
  ok: boolean;
  server: {
    name: string;
    version: string;
    started_at: string;
    bind?: string;
    auth_required?: boolean;
    tls_terminated?: boolean;
    ffmpeg?: boolean;
  };
  doctor: Record<string, unknown> & { ok: boolean };
  update?: UpdateInfo;
}

export interface ConfigResponse {
  schema: string;
  ok: true;
  redacted: true;
  config: Record<string, unknown>;
}

export interface AgentEntry {
  session_id: string;
  pane_id: string;
  agent?: string | null;
  status: string;
  revision?: number;
  workspace_id?: string | null;
  tab_id?: string | null;
  terminal_id?: string | null;
  cwd?: string | null;
  focused?: boolean;
  friendly_name?: string | null;
  pending_event_id?: string | null;
}

export interface SessionEntry {
  session_id: string;
  ok: boolean;
  label?: string | null;
  version?: string | null;
  protocol?: number | null;
  socket?: string | null;
  ssh?: string | null;
  agent_count?: number;
  error?: string | null;
  agents: AgentEntry[];
}

export interface SessionsResponse {
  schema: string;
  ok: boolean;
  sessions: SessionEntry[];
}

export interface ContextResponse {
  schema: string;
  ok: boolean;
  session_id: string;
  pane_id: string;
  lines?: number;
  revision?: number | null;
  text: string;
  pending_question?: {
    event_id: string;
    text?: string | null;
    choices?: string[] | null;
    fingerprint?: string | null;
    risk?: string | null;
  } | null;
}

export interface DeliveriesResponse {
  schema: string;
  ok: boolean;
  pending: BoundEvent[];
  recent: { event_id: string; status: string; ts: number; [k: string]: unknown }[];
}

export interface UsageResponse {
  schema: string;
  ok: boolean;
  summary: {
    tts: UsageAgg;
    stt: UsageAgg;
    total_events: number;
    [k: string]: unknown;
  };
  near_misses?: { count?: number; attempts?: unknown[]; observed_at?: string | null }[];
}

export interface UsageAgg {
  instances?: number;
  ok?: number;
  errors?: number;
  empty_stt_rate?: number;
  empty_transcript?: number;
  by_provider?: Record<string, number>;
  avg_latency_ms?: number;
  total_audio_s?: number;
  [k: string]: unknown;
}

export interface EventsPage {
  schema: string;
  ok: boolean;
  events: Envelope[];
  cursor: string;
  complete: boolean;
}

export interface AnswerResponse {
  ok: boolean;
  event_id: string;
  status: "delivered" | "rejected" | "uncertain";
  detail?: string | null;
}

export interface ApiError {
  ok: false;
  error: { code: string; message?: string };
}
