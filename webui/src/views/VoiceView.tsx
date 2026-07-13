import { useComputed } from "@preact/signals";
import { conferenceHold, health, usage, usePoll } from "../lib/data";
import { pct, sparkline } from "../lib/spark";
import { events } from "../lib/stream";
import type { UsageAgg, UsagePayload } from "../lib/types";

function Light({ on, hot, label }: { on: boolean; hot?: boolean; label: string }) {
  const color = on ? (hot ? "var(--warn)" : "var(--ok)") : "var(--muted)";
  return (
    <span style="white-space:nowrap">
      <span style={`color:${color}`}>{on ? "●" : "○"}</span>{" "}
      <span style={`color:${on ? "var(--text)" : "var(--text-faint)"}`}>{label}</span>
    </span>
  );
}

/** Recent per-call series for sparklines, from the live event buffer. */
function useUsageSeries(kind: "tts" | "stt") {
  return useComputed(() => {
    const rows = events.value
      .filter((e) => e.source === "usage")
      .map((e) => e.payload as UsagePayload)
      .filter((p) => p.kind === kind);
    return {
      latency: rows.map((p) => p.latency_ms ?? 0),
      audio: rows.map((p) => (p.audio_ms ?? 0) / 1000),
      errs: rows.map((p) => (p.ok ? 0 : 1)),
    };
  });
}

function UsageRow({ kind, agg }: { kind: "tts" | "stt"; agg: UsageAgg | undefined }) {
  const series = useUsageSeries(kind);
  if (!agg?.instances) {
    return (
      <div class="readout">
        <span class="rlabel">{kind}</span>
        <span class="dim">no calls yet</span>
      </div>
    );
  }
  const errs = agg.errors ?? 0;
  const emptyRate = (agg.empty_stt_rate as number) ?? 0;
  return (
    <>
      <div class="readout">
        <span class="rlabel">{kind}</span>
        <span class="v">{agg.instances}</span>
        <span class="dim">calls</span>
        <span class="sep">·</span>
        <span class={`v ${errs ? (errs / agg.instances! > 0.1 ? "err" : "warn") : "ok"}`}>
          {errs} err
        </span>
        {kind === "stt" && (
          <>
            <span class="sep">·</span>
            <span class={`v ${emptyRate > 0.15 ? "warn" : ""}`}>{pct(emptyRate)}</span>
            <span class="dim">empty</span>
          </>
        )}
        <span class="sep">·</span>
        <span class="dim">lat</span>
        <span class="v">{Math.round((agg.avg_latency_ms as number) ?? 0)}ms</span>
        <span style="color:var(--brand-hi);letter-spacing:-1px" title="recent latency">
          {sparkline(series.value.latency)}
        </span>
        <span class="sep">·</span>
        <span class="dim">audio</span>
        <span class="v">{Math.round((agg.total_audio_s as number) ?? 0)}s</span>
        <span class="sep">·</span>
        <span class="dim">
          {Object.entries(agg.by_provider ?? {})
            .map(([p, n]) => `${p}×${n}`)
            .join(" ")}
        </span>
      </div>
    </>
  );
}

/** What TTS actually said, newest last (usage stream meta.text_preview). */
function SpokenAudit() {
  const rows = useComputed(() =>
    events.value
      .filter((e) => e.source === "usage")
      .map((e) => e.payload as UsagePayload)
      .filter((p) => p.kind === "tts")
      .slice(-10),
  );
  if (!rows.value.length) return <span class="readout dim">nothing spoken in this window</span>;
  return (
    <>
      {rows.value.map((p, i) => (
        <div key={i} class="readout">
          <span class="dim" style="min-width:64px">
            {new Date(p.ts * 1000).toLocaleTimeString(undefined, { hour12: false })}
          </span>
          <span style="color:var(--text-dim);overflow-wrap:anywhere">
            {String((p.meta as { text_preview?: string })?.text_preview ?? `${p.chars} chars`)}
          </span>
          <span class="dim" style="margin-left:auto;white-space:nowrap">
            {p.voice ?? p.provider} · {((p.audio_ms ?? 0) / 1000).toFixed(1)}s
            {(p.meta as { from_cache?: boolean })?.from_cache ? " · cached" : ""}
          </span>
        </div>
      ))}
    </>
  );
}

export function VoiceView() {
  usePoll("health", 4000);
  usePoll("usage", 10000);
  const pipe = health.value?.pipeline;
  const u = usage.value;

  return (
    <div class="panelwrap">
      <section class="section">
        <h3 class="paneltitle">pipeline</h3>
        {!pipe ? (
          <span class="readout dim">loading…</span>
        ) : (
          <div class="readout" style="gap:16px">
            <Light on={!!pipe.mode_a?.running} label="mode-a" />
            <Light on={!!pipe.harkd?.running} label="harkd" />
            <Light on={!!pipe.mic_lock} hot label="mic-lease" />
            <Light on={!!pipe.busy_lock} hot label="recording" />
            <Light on={!!pipe.ambient_pause} hot label="ambient-pause" />
            <Light on={conferenceHold.value} hot label="conf-hold" />
            {(pipe.announce_hold_queued ?? 0) > 0 && (
              <span class="v warn">{pipe.announce_hold_queued} queued</span>
            )}
            <span class="dim" style="margin-left:auto;font-size:10.5px">
              {pipe.state_dir}
            </span>
          </div>
        )}
      </section>
      <section class="section">
        <h3 class="paneltitle">usage</h3>
        <UsageRow kind="tts" agg={u?.summary.tts} />
        <UsageRow kind="stt" agg={u?.summary.stt} />
      </section>
      <section class="section">
        <h3 class="paneltitle">spoken (tts audit)</h3>
        <SpokenAudit />
      </section>
      <section class="section">
        <h3 class="paneltitle">wake near-misses</h3>
        {u?.near_misses?.length ? (
          u.near_misses.slice(-8).map((m, i) => (
            <div key={i} class="readout">
              <span class="v warn">{m.count ?? 1}×</span>
              <span style="overflow-wrap:anywhere;color:var(--text-dim)">
                {(m.attempts ?? [])
                  .map((a) =>
                    typeof a === "object" && a ? ((a as { text?: string }).text ?? "") : String(a),
                  )
                  .filter(Boolean)
                  .join(" · ")}
              </span>
              <span class="dim" style="margin-left:auto">
                {m.observed_at
                  ? new Date(m.observed_at).toLocaleTimeString(undefined, { hour12: false })
                  : ""}
              </span>
            </div>
          ))
        ) : (
          <span class="readout dim">none — wake matching is healthy</span>
        )}
      </section>
    </div>
  );
}
