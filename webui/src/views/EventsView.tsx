import { effect, useComputed, useSignal } from "@preact/signals";
import { useEffect, useRef } from "preact/hooks";
import { EventCard } from "../components/EventCard";
import { Timeline } from "../components/Timeline";
import { focusEventId } from "../lib/notify";
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

interface SavedView {
  name: string;
  query: string;
  sources: string[];
  sevs: Severity[];
}

const VIEWS_KEY = "hark.savedViews";

const loadViews = (): SavedView[] => {
  try {
    return JSON.parse(localStorage.getItem(VIEWS_KEY) ?? "[]");
  } catch {
    return [];
  }
};

export function EventsView() {
  const query = useSignal("");
  const srcOn = useSignal<Set<string>>(new Set(SOURCES));
  const sevOn = useSignal<Set<Severity>>(new Set(SEVS.map((s) => s.id)));
  const follow = useSignal(true);
  const saved = useSignal<SavedView[]>(loadViews());
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

  useEffect(() => {
    const el = feedRef.current;
    if (el && follow.value && !paused.value) el.scrollTop = el.scrollHeight;
  }, [filtered.value, follow.value]);

  // notification deep-link: scroll to + flash the event card
  useEffect(
    () =>
      effect(() => {
        const eid = focusEventId.value;
        if (!eid) return;
        focusEventId.value = null;
        window.setTimeout(() => {
          const el = feedRef.current?.querySelector(`[data-eid="${CSS.escape(eid)}"]`);
          if (el) {
            follow.value = false;
            el.scrollIntoView({ block: "center" });
            el.animate(
              [{ outline: "2px solid var(--brand-hi)" }, { outline: "2px solid transparent" }],
              { duration: 1600 },
            );
          }
        }, 80);
      }),
    [],
  );

  const jumpTo = (cursor: string) => {
    const el = feedRef.current?.querySelector(`[data-cursor="${CSS.escape(cursor)}"]`);
    if (el) {
      follow.value = false;
      el.scrollIntoView({ block: "start" });
    }
  };

  const toggle = <T,>(sig: typeof srcOn | typeof sevOn, v: T) => {
    const next = new Set(sig.value as Set<T>);
    next.has(v) ? next.delete(v) : next.add(v);
    sig.value = next as never;
  };

  const persistViews = (views: SavedView[]) => {
    saved.value = views;
    localStorage.setItem(VIEWS_KEY, JSON.stringify(views));
  };

  const saveCurrent = () => {
    const name = window.prompt("name this view", "blocked only");
    if (!name) return;
    persistViews([
      ...saved.value.filter((v) => v.name !== name),
      { name, query: query.value, sources: [...srcOn.value], sevs: [...sevOn.value] },
    ]);
  };

  const applyView = (v: SavedView) => {
    query.value = v.query;
    srcOn.value = new Set(v.sources);
    sevOn.value = new Set(v.sevs);
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
        <span style="width:1px;height:18px;background:var(--line)" />
        {saved.value.map((v) => (
          <button
            key={v.name}
            class="chip"
            title="apply saved view (shift-click to delete)"
            onClick={(e) =>
              e.shiftKey
                ? persistViews(saved.value.filter((x) => x.name !== v.name))
                : applyView(v)
            }
          >
            ★ {v.name}
          </button>
        ))}
        <button class="chip" title="save current filters as a view" onClick={saveCurrent}>
          + save view
        </button>
        <button
          class={`btn small ${paused.value ? "primary" : ""}`}
          onClick={() => setPaused(!paused.value)}
          title="pause/resume the live tail"
        >
          {paused.value ? `▶ resume (+${pausedCount.value})` : "⏸ pause"}
        </button>
      </div>
      <Timeline events={filtered.value} onJump={jumpTo} />
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
