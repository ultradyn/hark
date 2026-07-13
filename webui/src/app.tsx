import { useComputed, useSignal } from "@preact/signals";
import { useEffect } from "preact/hooks";
import { api } from "./lib/api";
import { connState, connect, events, serverInfo, severityOf } from "./lib/stream";
import { EventsView } from "./views/EventsView";

export type ViewId = "events";

const VIEWS: { id: ViewId; label: string; icon: string }[] = [
  { id: "events", label: "events", icon: "≋" },
];

function ConnBadge() {
  const state = connState.value;
  const cls =
    state === "live" ? "live" : state === "fixtures" ? "fixtures" : state === "auth" ? "err" : "warn";
  const label =
    state === "live"
      ? "on air"
      : state === "fixtures"
        ? "fixtures"
        : state === "auth"
          ? "auth required"
          : state;
  return (
    <span class={`badge ${cls}`} title={`stream: ${state}`}>
      <span class="dot" /> {label}
    </span>
  );
}

function AuthGate() {
  const token = useSignal("");
  const error = useSignal<string | null>(null);
  const submit = async (e: Event) => {
    e.preventDefault();
    try {
      await api.auth(token.value.trim());
      error.value = null;
      connect();
    } catch {
      error.value = "bad token";
    }
  };
  return (
    <div class="empty">
      <span class="glyph">⌖</span>
      <p>this hark serve requires a token</p>
      <form onSubmit={submit} style="display:flex;gap:8px;justify-content:center;margin-top:12px">
        <input
          class="input"
          type="password"
          placeholder="[dashboard].token"
          value={token.value}
          onInput={(e) => (token.value = (e.target as HTMLInputElement).value)}
        />
        <button class="btn primary" type="submit">
          unlock
        </button>
      </form>
      {error.value && <p style="color:var(--error);margin-top:10px">{error.value}</p>}
    </div>
  );
}

export function App() {
  const view = useSignal<ViewId>("events");
  useEffect(() => connect(), []);

  const blockedCount = useComputed(
    () =>
      events.value.filter((e) => {
        const k = (e.payload as { kind?: string }).kind;
        return k === "agent.blocked" || k === "agent.needs_input";
      }).length,
  );
  const errorCount = useComputed(
    () => events.value.filter((e) => severityOf(e) === "error").length,
  );

  return (
    <div class="shell">
      <header class="topbar">
        <div class="wordmark">
          <span class="glyph">⁂</span> hark <small>dashboard</small>
        </div>
        <ConnBadge />
        {blockedCount.value > 0 && (
          <span class="badge warn" title="agents waiting for input">
            ⚠ {blockedCount.value} blocked
          </span>
        )}
        {errorCount.value > 0 && (
          <span class="badge err">{errorCount.value} errors</span>
        )}
        <span style="margin-left:auto;color:var(--text-faint);font-size:11px">
          {serverInfo.value ? `${serverInfo.value.server} ${serverInfo.value.version}` : ""}
        </span>
      </header>
      <div class="main">
        <nav class="sidenav">
          <div class="navlabel">console</div>
          {VIEWS.map((v) => (
            <button
              key={v.id}
              class={`navitem ${view.value === v.id ? "active" : ""}`}
              onClick={() => (view.value = v.id)}
            >
              <span>{v.icon}</span> {v.label}
              {v.id === "events" && (
                <span class={`count ${blockedCount.value ? "hot" : ""}`}>
                  {blockedCount.value ? `${blockedCount.value}⚠` : events.value.length}
                </span>
              )}
            </button>
          ))}
        </nav>
        <main class="content">
          {connState.value === "auth" ? <AuthGate /> : view.value === "events" && <EventsView />}
        </main>
      </div>
    </div>
  );
}
