import { useSignal } from "@preact/signals";
import { useEffect } from "preact/hooks";
import { api } from "../lib/api";
import { conferenceHold, selectedPane, sessions, usePoll } from "../lib/data";
import { sparkline } from "../lib/spark";
import { events, timeOf } from "../lib/stream";
import type { AgentEntry, ContextResponse } from "../lib/types";

const STATUS_COLOR: Record<string, string> = {
  blocked: "var(--warn)",
  needs_input: "var(--warn)",
  working: "var(--brand-hi)",
  running: "var(--brand-hi)",
  done: "var(--ok)",
  idle: "var(--muted)",
};

function StatusPill({ status }: { status: string }) {
  const color = STATUS_COLOR[status] ?? "var(--info)";
  const hot = status === "blocked" || status === "needs_input";
  return (
    <span
      class="badge"
      style={`border-color:${color}55;color:${color};${hot ? "font-weight:700" : ""}`}
    >
      {hot && <span class="dot" style={`background:${color}`} />}
      {status}
    </span>
  );
}

function PaneDrawer({
  agent,
  onClose,
}: {
  agent: AgentEntry;
  onClose: () => void;
}) {
  const ctx = useSignal<ContextResponse | null>(null);
  const answer = useSignal("");
  const sending = useSignal(false);
  const result = useSignal<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await api.context(agent.session_id, agent.pane_id, 80);
        if (alive) ctx.value = res;
      } catch {
        /* pane may be gone */
      }
    };
    void load();
    const t = window.setInterval(load, 3000);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, [agent.session_id, agent.pane_id]);

  const pending = ctx.value?.pending_question;

  const send = async () => {
    if (!pending || !answer.value.trim() || sending.value) return;
    sending.value = true;
    result.value = null;
    try {
      const res = await api.answer(pending.event_id, { text: answer.value.trim() });
      result.value =
        res.status === "delivered" ? "✓ delivered" : `✗ ${res.detail ?? res.status}`;
      if (res.status === "delivered") answer.value = "";
    } catch (err) {
      result.value = `✗ ${(err as Error).message || "failed"}`;
    } finally {
      sending.value = false;
    }
  };

  return (
    <div class="card" style="padding:14px;display:flex;flex-direction:column;gap:10px;min-height:0">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <b>
          {agent.session_id}/{agent.pane_id}
        </b>
        {agent.agent && <span class="badge">{agent.agent}</span>}
        {agent.friendly_name && <span style="color:var(--text-dim)">{agent.friendly_name}</span>}
        <StatusPill status={agent.status} />
        <span style="color:var(--text-faint);font-size:11px">
          rev {ctx.value?.revision ?? agent.revision ?? "?"}
        </span>
        <button class="btn small" style="margin-left:auto" onClick={onClose}>
          ✕ close
        </button>
      </div>
      <pre
        style="margin:0;background:#05070d;border:1px solid var(--line-soft);border-radius:10px;
               padding:12px;overflow:auto;max-height:44vh;font-size:11.5px;color:#c3cee8;white-space:pre-wrap"
      >
        {ctx.value?.text ?? "loading pane…"}
      </pre>
      {pending && (
        <div style="display:flex;flex-direction:column;gap:8px">
          <div class="qtext" style="margin:0">
            {pending.risk && <span class={`risk ${pending.risk.toLowerCase()}`}>{pending.risk} </span>}
            {pending.text ?? "(pending question)"}
          </div>
          {pending.choices?.length ? (
            <div style="display:flex;gap:6px;flex-wrap:wrap">
              {pending.choices.map((c, i) => (
                <button
                  key={c}
                  class="btn small"
                  disabled={sending.value}
                  onClick={async () => {
                    if (!window.confirm(`answer "${c}"?`)) return;
                    sending.value = true;
                    try {
                      const res = await api.answer(pending.event_id, {
                        keys: [String(i + 1), "enter"],
                      });
                      result.value =
                        res.status === "delivered" ? `✓ ${c}` : `✗ ${res.detail ?? res.status}`;
                    } catch (err) {
                      result.value = `✗ ${(err as Error).message || "failed"}`;
                    } finally {
                      sending.value = false;
                    }
                  }}
                >
                  {i + 1} · {c}
                </button>
              ))}
            </div>
          ) : null}
          <div style="display:flex;gap:8px">
            <input
              class="input"
              style="flex:1"
              placeholder="type an answer — delivered via bound, fingerprint-checked send"
              value={answer.value}
              onInput={(e) => (answer.value = (e.target as HTMLInputElement).value)}
              onKeyDown={(e) => e.key === "Enter" && send()}
            />
            <button class="btn primary" disabled={sending.value} onClick={send}>
              answer
            </button>
          </div>
          {result.value && (
            <span
              style={`color:${result.value.startsWith("✓") ? "var(--ok)" : "var(--error)"};font-size:12px`}
            >
              {result.value}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

/** events-per-pane over the buffer window, bucketed by time for a sparkline */
function paneActivity(session_id: string, pane_id: string): string {
  const times: number[] = [];
  for (const e of events.value) {
    const p = e.payload as { session_id?: string; target?: { pane_id?: string }; pane_id?: string };
    const pane = p.target?.pane_id ?? p.pane_id;
    if (pane === pane_id && (p.session_id ?? session_id) === session_id) {
      const t = timeOf(e)?.getTime();
      if (t) times.push(t);
    }
  }
  if (times.length < 2) return "";
  const t0 = Math.min(...times);
  const span = Math.max(Math.max(...times) - t0, 1);
  const buckets = new Array(16).fill(0);
  for (const t of times) buckets[Math.min(15, Math.floor(((t - t0) / span) * 16))] += 1;
  return sparkline(buckets, 16);
}

export function HerdrView() {
  usePoll("sessions", 5000);
  const data = sessions.value;
  const selected = data?.sessions
    .flatMap((s) => s.agents)
    .find(
      (a) =>
        a.session_id === selectedPane.value?.session_id &&
        a.pane_id === selectedPane.value?.pane_id,
    );

  return (
    <div class="panelwrap">
      {conferenceHold.value && (
        <div class="readout" style="color:var(--warn)">
          ⏸ conference hold active — announcements are queued, agents still tracked
        </div>
      )}
      {selected && <PaneDrawer agent={selected} onClose={() => (selectedPane.value = null)} />}
      {!data && <div class="empty">loading sessions…</div>}
      {data?.sessions.map((s) => (
        <section key={s.session_id} class="section">
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px">
            <b>{s.session_id}</b>
            {s.label && <span style="color:var(--text-dim)">{s.label}</span>}
            {s.ssh && <span class="badge">ssh {s.ssh}</span>}
            {s.ok ? (
              <span class="badge live">
                <span class="dot" /> herdr {s.version ?? ""}
              </span>
            ) : (
              <span class="badge err">✗ {s.error ?? "unreachable"}</span>
            )}
            <span style="margin-left:auto;color:var(--text-faint);font-size:11px">
              {s.agents.length} agents
            </span>
          </div>
          {s.agents.length === 0 ? (
            <div style="color:var(--text-faint)">no panes</div>
          ) : (
            <div style="display:flex;flex-direction:column;gap:0">
              {s.agents.map((a) => (
                <button
                  key={a.pane_id}
                  class="panerow"
                  style={`--prow:${STATUS_COLOR[a.status] ?? "var(--info)"}`}
                  onClick={() => (selectedPane.value = { session_id: a.session_id, pane_id: a.pane_id })}
                >
                  <span style="min-width:70px">{a.pane_id}</span>
                  <span style="color:var(--text)">{a.agent ?? "?"}</span>
                  {a.friendly_name && (
                    <span style="color:var(--text-faint)">· {a.friendly_name}</span>
                  )}
                  {a.cwd && (
                    <span
                      style="color:var(--text-faint);font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:26ch"
                    >
                      {a.cwd}
                    </span>
                  )}
                  <span
                    style="margin-left:auto;color:var(--brand-hi);letter-spacing:-1px;font-size:10px"
                    title="event activity (buffer window)"
                  >
                    {paneActivity(a.session_id, a.pane_id)}
                  </span>
                  {a.pending_event_id && (
                    <span class="badge warn" title="pending bound question">
                      awaiting answer
                    </span>
                  )}
                  {a.focused && <span class="badge">focused</span>}
                  <StatusPill status={a.status} />
                </button>
              ))}
            </div>
          )}
        </section>
      ))}
    </div>
  );
}
