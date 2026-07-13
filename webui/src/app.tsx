import { useComputed, useSignal } from "@preact/signals";
import { useEffect } from "preact/hooks";
import { api } from "./lib/api";
import { conferenceHold, deliveries, wireDataRefreshes } from "./lib/data";
import { connState, connect, events, serverInfo, severityOf } from "./lib/stream";
import { DictateOverlay } from "./components/Dictate";
import { Palette } from "./components/Palette";
import { focusEventId, notifyEnabled, notifySupported, toggleNotifications, wireNotifications } from "./lib/notify";
import { selectedPane } from "./lib/data";
import { EventsView } from "./views/EventsView";
import { HealthView } from "./views/HealthView";
import { HerdrView } from "./views/HerdrView";
import { QueueView } from "./views/QueueView";
import { VoiceView } from "./views/VoiceView";

export type ViewId = "events" | "herdr" | "queue" | "voice" | "health";

const VIEWS: { id: ViewId; label: string; icon: string }[] = [
  { id: "events", label: "events", icon: "≋" },
  { id: "herdr", label: "herdr", icon: "⛶" },
  { id: "queue", label: "queue", icon: "⇶" },
  { id: "voice", label: "voice", icon: "◉" },
  { id: "health", label: "health", icon: "♥" },
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
  const dictating = useSignal(false);
  const paletteOpen = useSignal(false);
  useEffect(() => {
    wireDataRefreshes();
    wireNotifications();
    connect();
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        paletteOpen.value = !paletteOpen.value;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // notification deep-link lands on the events view
  useEffect(() => {
    const unsub = focusEventId.subscribe((v) => {
      if (v) view.value = "events";
    });
    return unsub;
  }, []);

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
        {conferenceHold.value && <span class="badge warn">⏸ conference</span>}
        {errorCount.value > 0 && (
          <span class="badge err">{errorCount.value} errors</span>
        )}
        <button
          class="btn small"
          style="margin-left:auto"
          onClick={() => (dictating.value = true)}
          title="dictate a prompt or answer by voice"
        >
          ◉ dictate
        </button>
        <button
          class="btn small"
          onClick={() => (paletteOpen.value = true)}
          title="command palette (ctrl/cmd-K)"
        >
          ⌘K
        </button>
        {notifySupported && (
          <button
            class={`btn small ${notifyEnabled.value ? "primary" : ""}`}
            onClick={() => void toggleNotifications()}
            title="notify when an agent blocks (needs HTTPS or localhost)"
          >
            {notifyEnabled.value ? "🔔" : "🔕"}
          </button>
        )}
        <span class="serverver" style="color:var(--text-faint);font-size:11px">
          {serverInfo.value ? `${serverInfo.value.server} ${serverInfo.value.version}` : ""}
        </span>
      </header>
      {dictating.value && <DictateOverlay onClose={() => (dictating.value = false)} />}
      {paletteOpen.value && (
        <Palette
          onView={(v) => (view.value = v)}
          onDictate={() => (dictating.value = true)}
          onPane={(session_id, pane_id) => {
            selectedPane.value = { session_id, pane_id };
            view.value = "herdr";
          }}
          onClose={() => (paletteOpen.value = false)}
        />
      )}
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
              {v.id === "queue" && (deliveries.value?.pending.length ?? 0) > 0 && (
                <span class="count hot">{deliveries.value!.pending.length}</span>
              )}
            </button>
          ))}
        </nav>
        <main class="content">
          {connState.value === "auth" ? (
            <AuthGate />
          ) : view.value === "events" ? (
            <EventsView />
          ) : view.value === "herdr" ? (
            <HerdrView />
          ) : view.value === "queue" ? (
            <QueueView />
          ) : view.value === "voice" ? (
            <VoiceView />
          ) : (
            <HealthView />
          )}
        </main>
      </div>
    </div>
  );
}
