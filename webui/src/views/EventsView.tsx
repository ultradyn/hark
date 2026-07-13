import { useComputed, useSignal } from "@preact/signals";
import { useEffect, useRef } from "preact/hooks";
import { EventCard } from "../components/EventCard";
import {
  events,
  kindOf,
  paused,
  pausedCount,
  setPaused,
  severityOf,
  type Severity,
} from "../lib/stream";

const SOURCES = ["watch", "ambient", "system", "usage", "delivery", "serve"] as const;
const SEVS: { id: Severity; color: string }[] = [
  { id: "error", color: "var(--error)" },
  { id: "warn", color: "var(--warn)" },
  { id: "ok", color: "var(--ok)" },
  { id: "ambient", color: "var(--ambient)" },
  { id: "info", color: "var(--info)" },
  { id: "muted", color: "var(--muted)" },
];

export function EventsView() {
  const query = useSignal("");
  const srcOn = useSignal<Set<string>>(new Set(SOURCES));
  const sevOn = useSignal<Set<Severity>>(new Set(SEVS.map((s) => s.id)));
  const follow = useSignal(true);
  const feedRef = useRef<HTMLDivElement>(null);

  const filtered = useComputed(() => {
    const q = query.value.toLowerCase().trim();
    return events.value.filter((e) => {
      if (!srcOn.value.has(e.source)) return false;
      if (!sevOn.value.has(severityOf(e))) return false;
      if (!q) return true;
      return (kindOf(e) + JSON.stringify(e.payload)).toLowerCase().includes(q);
    });
  });

  // stick to bottom while following
  useEffect(() => {
    const el = feedRef.current;
    if (el && follow.value && !paused.value) el.scrollTop = el.scrollHeight;
  }, [filtered.value, follow.value]);

  const toggle = <T,>(sig: typeof srcOn | typeof sevOn, v: T) => {
    const next = new Set(sig.value as Set<T>);
    next.has(v) ? next.delete(v) : next.add(v);
    (sig.value as Set<T>) = next as never;
    sig.value = next as never;
  };

  return (
    <div class="feedwrap">
      <div class="feedbar">
        <input
          class="input"
          style="flex:1;min-width:140px"
          placeholder="search kinds, text, targets…"
          value={query.value}
          onInput={(e) => (query.value = (e.target as HTMLInputElement).value)}
        />
        {SOURCES.map((s) => (
          <button
            key={s}
            class={`chip ${srcOn.value.has(s) ? "on" : ""}`}
            onClick={() => toggle(srcOn, s)}
          >
            {s}
          </button>
        ))}
        <span style="width:1px;height:18px;background:var(--line)" />
        {SEVS.map((s) => (
          <button
            key={s.id}
            class={`chip ${sevOn.value.has(s.id) ? "on" : ""}`}
            style={`--chip:${s.color}`}
            onClick={() => toggle(sevOn, s.id)}
          >
            {s.id}
          </button>
        ))}
        <button
          class={`btn small ${paused.value ? "primary" : ""}`}
          onClick={() => setPaused(!paused.value)}
          title="pause/resume the live tail"
        >
          {paused.value ? `▶ resume (+${pausedCount.value})` : "⏸ pause"}
        </button>
      </div>
      <div
        class="feed"
        ref={feedRef}
        onScroll={() => {
          const el = feedRef.current!;
          follow.value = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
        }}
      >
        {filtered.value.length === 0 ? (
          <div class="empty">
            <span class="glyph">⁂</span>
            waiting for events…
            <br />
            <span style="font-size:11px">
              arm Mode A (<code>hark watch</code> / <code>hark ambient</code>) and the feed lights
              up
            </span>
          </div>
        ) : (
          filtered.value.map((e, i) => <EventCard key={`${e.cursor}-${i}`} e={e} />)
        )}
      </div>
    </div>
  );
}
