import { useSignal } from "@preact/signals";
import { api } from "../lib/api";
import { deliveries, refresh, usePoll } from "../lib/data";
import type { BoundEvent } from "../lib/types";

const OUTCOME_COLOR: Record<string, string> = {
  delivered: "var(--ok)",
  rejected: "var(--error)",
  invalidated: "var(--error)",
  skipped: "var(--muted)",
  uncertain: "var(--warn)",
};

function PendingRow({ ev }: { ev: BoundEvent }) {
  const answer = useSignal("");
  const busy = useSignal(false);
  const note = useSignal<string | null>(null);

  const act = async (opts: { text?: string; keys?: string[] }) => {
    if (busy.value) return;
    busy.value = true;
    note.value = null;
    try {
      const res = await api.answer(ev.event_id, opts);
      note.value = res.status === "delivered" ? "✓ delivered" : `✗ ${res.detail ?? res.status}`;
      void refresh("deliveries");
    } catch (err) {
      note.value = `✗ ${(err as Error).message || "failed"}`;
    } finally {
      busy.value = false;
    }
  };

  return (
    <div class="event" style="--sev:var(--warn)">
      <div class="head">
        <span class="kind">pending</span>
        <span class="target">
          {ev.session_id}/{ev.pane_id}
        </span>
        {ev.risk && <span class={`risk ${ev.risk.toLowerCase()}`}>{ev.risk}</span>}
        <span class="when">
          {ev.created_at ? new Date(ev.created_at * 1000).toLocaleTimeString(undefined, { hour12: false }) : ""}
        </span>
      </div>
      {ev.question_text && <div class="qtext">{ev.question_text}</div>}
      <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
        <input
          class="input"
          style="flex:1;min-width:160px"
          placeholder="answer text…"
          value={answer.value}
          onInput={(e) => (answer.value = (e.target as HTMLInputElement).value)}
          onKeyDown={(e) =>
            e.key === "Enter" && answer.value.trim() && act({ text: answer.value.trim() })
          }
        />
        <button
          class="btn primary small"
          disabled={busy.value || !answer.value.trim()}
          onClick={() => act({ text: answer.value.trim() })}
        >
          send
        </button>
      </div>
      {note.value && (
        <div
          style={`margin-top:6px;font-size:12px;color:${note.value.startsWith("✓") ? "var(--ok)" : "var(--error)"}`}
        >
          {note.value}
        </div>
      )}
    </div>
  );
}

export function QueueView() {
  usePoll("deliveries", 6000);
  const data = deliveries.value;

  return (
    <div class="panelwrap">
      <section class="section">
        <h3 class="paneltitle">pending answers ({data?.pending.length ?? "…"})</h3>
        {data?.pending.length ? (
          <div style="display:flex;flex-direction:column;gap:8px">
            {data.pending.map((ev) => (
              <PendingRow key={ev.event_id} ev={ev} />
            ))}
          </div>
        ) : (
          <span class="readout dim">queue is clear — no agents waiting on a bound answer</span>
        )}
      </section>
      <section class="section">
        <h3 class="paneltitle">recent outcomes</h3>
        <div style="display:flex;flex-direction:column;gap:2px">
          {data?.recent.length ? (
            data.recent
              .slice()
              .reverse()
              .slice(0, 40)
              .map((r, i) => (
                <div key={`${r.event_id}-${i}`} style="display:flex;gap:10px;font-size:12px">
                  <span style={`color:${OUTCOME_COLOR[r.status] ?? "var(--info)"};min-width:86px;font-weight:600`}>
                    {r.status}
                  </span>
                  <span style="color:var(--text-dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                    {r.event_id}
                  </span>
                  {typeof r.reason === "string" && (
                    <span style="color:var(--text-faint)">{r.reason}</span>
                  )}
                  <span style="margin-left:auto;color:var(--text-faint)">
                    {new Date(r.ts * 1000).toLocaleTimeString(undefined, { hour12: false })}
                  </span>
                </div>
              ))
          ) : (
            <span class="readout dim">no outcomes yet</span>
          )}
        </div>
      </section>
    </div>
  );
}
