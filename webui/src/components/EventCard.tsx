import { useSignal } from "@preact/signals";
import { api } from "../lib/api";
// EventCard renders one dense log row (flat, hairline-separated — not a card).
import { kindOf, severityOf, timeOf } from "../lib/stream";
import type { Envelope, HepPayload, LogPayload, UsagePayload } from "../lib/types";

const SEV_COLOR: Record<string, string> = {
  error: "var(--error)",
  warn: "var(--warn)",
  ok: "var(--ok)",
  ambient: "var(--ambient)",
  info: "var(--info)",
  muted: "var(--muted)",
};

function fmtTime(d: Date | null): string {
  if (!d || Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString(undefined, { hour12: false });
}

function targetOf(e: Envelope): string | null {
  const p = e.payload as HepPayload & { session_id?: string; pane_id?: string };
  if (p.target?.pane_id) {
    const name = p.target.friendly_name ? ` · ${p.target.friendly_name}` : "";
    return `${p.session_id ?? ""}/${p.target.pane_id}${p.target.agent ? ` · ${p.target.agent}` : ""}${name}`;
  }
  if (p.pane_id) return `${p.session_id ?? ""}/${p.pane_id}`;
  return null;
}

/** One-tap bound answer for menu-choice events (fingerprint-checked server side). */
function Choices({ e }: { e: Envelope }) {
  const p = e.payload as HepPayload;
  const busy = useSignal(false);
  const result = useSignal<string | null>(null);
  if (!p.question?.choices?.length || !p.event_id) return null;
  if (p.kind !== "agent.blocked" && p.kind !== "agent.needs_input") return null;

  const answer = async (idx: number, label: string) => {
    if (busy.value) return;
    if (!window.confirm(`answer "${label}" to ${targetOf(e) ?? p.event_id}?`)) return;
    busy.value = true;
    try {
      const res = await api.answer(p.event_id!, { keys: [String(idx + 1), "enter"] });
      result.value = res.status === "delivered" ? `✓ ${label}` : `✗ ${res.detail ?? res.status}`;
    } catch (err) {
      result.value = `✗ ${(err as Error).message || "failed"}`;
    } finally {
      busy.value = false;
    }
  };

  return (
    <div class="choices">
      {result.value ? (
        <span style={`color:${result.value.startsWith("✓") ? "var(--ok)" : "var(--error)"}`}>
          {result.value}
        </span>
      ) : (
        p.question.choices.map((c, i) => (
          <button key={c} class="btn small" disabled={busy.value} onClick={() => answer(i, c)}>
            {i + 1} · {c}
          </button>
        ))
      )}
    </div>
  );
}

function Body({ e }: { e: Envelope }) {
  const p = e.payload as HepPayload & LogPayload & UsagePayload & { type?: string; status?: string };
  if (e.source === "system") {
    return (
      <div class="body">
        <b>{p.component}</b> {p.message}
      </div>
    );
  }
  if (e.source === "usage") {
    return (
      <div class="body">
        {p.provider ?? "?"} · {p.words ?? 0}w · {p.audio_ms ?? 0}ms audio · {p.latency_ms ?? 0}ms
        {p.error ? <b> · {p.error}</b> : null}
      </div>
    );
  }
  if (e.source === "delivery") {
    const d = e.payload as { type?: string; status?: string; question_text?: string | null };
    return (
      <div class="body">
        {d.type === "outcome" ? `outcome: ${d.status}` : (d.question_text ?? "bound event")}
      </div>
    );
  }
  const hep = e.payload as HepPayload;
  const q = p.question?.text;
  const risk = p.question?.risk?.toLowerCase();
  return (
    <>
      {q && (
        <div class="qtext">
          {risk && <span class={`risk ${risk}`}>{p.question!.risk} </span>}
          {q}
        </div>
      )}
      {typeof p.text === "string" && p.text && !q && <div class="body">“{p.text}”</div>}
      {p.error != null && <div class="body">{String(p.error)}</div>}
      {hep.kind === "ambient.wake_near_miss" && Array.isArray(p.attempts) && (
        <div class="body">
          attempts:{" "}
          {p.attempts
            .map((a) => (typeof a === "object" && a ? (a as { text?: string }).text : String(a)))
            .filter(Boolean)
            .join(" · ")}
        </div>
      )}
      <Choices e={e} />
    </>
  );
}

export function EventCard({ e }: { e: Envelope }) {
  const sev = severityOf(e);
  const eid = (e.payload as { event_id?: string }).event_id;
  const showRaw = useSignal(false);
  return (
    <article class="event" style={`--sev:${SEV_COLOR[sev]}`} data-eid={eid} data-cursor={e.cursor}>
      <div class="head">
        <span class="kind">{kindOf(e)}</span>
        <span class="src">{e.source}</span>
        {targetOf(e) && <span class="target">{targetOf(e)}</span>}
        <span class="when">{fmtTime(timeOf(e))}</span>
        <button
          class="rawtoggle"
          aria-label="toggle raw payload"
          title="raw payload"
          onClick={() => (showRaw.value = !showRaw.value)}
        >
          {showRaw.value ? "▾" : "▸"}
        </button>
      </div>
      <Body e={e} />
      {showRaw.value && <pre class="rawpre">{JSON.stringify(e.payload, null, 2)}</pre>}
    </article>
  );
}
