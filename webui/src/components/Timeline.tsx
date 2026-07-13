import { timeOf } from "../lib/stream";
import type { Envelope } from "../lib/types";

const BUCKETS = 60;

/** Clickable event-density strip over the buffer window (timeline scrubber).
 * Computed per render — `events` is a prop, not a signal, so useComputed
 * would freeze over the first render's (empty) array. */
export function Timeline({
  events,
  onJump,
}: {
  events: Envelope[];
  onJump: (cursor: string) => void;
}) {
  const model = (() => {
    const stamped = events
      .map((e) => ({ e, t: timeOf(e)?.getTime() ?? 0 }))
      .filter((x) => x.t > 0);
    if (stamped.length < 2) return null;
    const t0 = stamped[0].t;
    const t1 = stamped[stamped.length - 1].t;
    const span = Math.max(t1 - t0, 1);
    const buckets = Array.from({ length: BUCKETS }, () => ({
      count: 0,
      warn: 0,
      first: null as string | null,
      t: 0,
    }));
    for (const { e, t } of stamped) {
      const i = Math.min(BUCKETS - 1, Math.floor(((t - t0) / span) * BUCKETS));
      const b = buckets[i];
      b.count += 1;
      b.t = t;
      if (!b.first) b.first = e.cursor;
      const kind = (e.payload as { kind?: string }).kind ?? "";
      if (kind === "agent.blocked" || kind === "agent.needs_input") b.warn += 1;
    }
    const max = Math.max(...buckets.map((b) => b.count), 1);
    return { buckets, max, t0, t1 };
  })();

  if (!model) return null;
  const { buckets, max, t0, t1 } = model;
  const fmt = (t: number) =>
    new Date(t).toLocaleTimeString(undefined, { hour12: false, hour: "2-digit", minute: "2-digit" });

  return (
    <div style="padding:6px var(--pad) 8px;display:flex;align-items:flex-end;gap:8px">
      <span style="color:var(--text-faint);font-size:10px">{fmt(t0)}</span>
      <div style="flex:1;display:flex;align-items:flex-end;gap:1px;height:26px">
        {buckets.map((b, i) => (
          <div
            key={i}
            title={b.count ? `${b.count} events · ${fmt(b.t)}` : ""}
            onClick={() => b.first && onJump(b.first)}
            style={`flex:1;min-width:2px;border-radius:1px 1px 0 0;cursor:${b.first ? "pointer" : "default"};
                    height:${b.count ? Math.max(8, (b.count / max) * 100) : 4}%;
                    background:${b.warn ? "var(--warn)" : b.count ? "var(--brand)" : "var(--line-soft)"};
                    opacity:${b.count ? 0.85 : 0.5}`}
          />
        ))}
      </div>
      <span style="color:var(--text-faint);font-size:10px">{fmt(t1)}</span>
    </div>
  );
}
