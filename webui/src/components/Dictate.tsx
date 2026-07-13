import { useSignal } from "@preact/signals";
import { useEffect, useRef } from "preact/hooks";
import { api, ApiRequestError } from "../lib/api";
import { deliveries, refresh } from "../lib/data";
import { onEnvelope } from "../lib/stream";

type Phase = "idle" | "recording" | "transcribing" | "review" | "submitted" | "failed";

const canBrowserMic = (): boolean =>
  window.isSecureContext && !!navigator.mediaDevices?.getUserMedia;

/** Mic capture overlay: record (browser or host) → review → safe submit. */
export function DictateOverlay({ onClose }: { onClose: () => void }) {
  const phase = useSignal<Phase>("idle");
  const mode = useSignal<"browser" | "host">(canBrowserMic() ? "browser" : "host");
  const text = useSignal("");
  const partial = useSignal("");
  const error = useSignal<string | null>(null);
  const target = useSignal<string>("prompt"); // "prompt" | event_id

  const recRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const rafRef = useRef(0);
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    void refresh("deliveries");
    // host-mode progress arrives on the stream
    return onEnvelope((e) => {
      if (e.source !== "serve") return;
      const p = e.payload as { kind?: string; state?: string; text?: string | null; error?: string | null; partial?: boolean };
      if (p.kind !== "serve.dictation" || mode.value !== "host") return;
      if (p.state === "recording" && p.partial && p.text) partial.value = p.text;
      else if (p.state === "done") {
        text.value = p.text ?? "";
        phase.value = "review";
      } else if (p.state === "failed") {
        error.value = p.error ?? "capture failed";
        phase.value = "failed";
      } else if (p.state === "cancelled") {
        phase.value = "idle";
        partial.value = "";
      }
    });
  }, []);

  const cleanupBrowser = () => {
    window.cancelAnimationFrame(rafRef.current);
    streamRef.current?.getTracks().forEach((t) => t.stop());
    void audioCtxRef.current?.close().catch(() => {});
    streamRef.current = null;
    audioCtxRef.current = null;
    recRef.current = null;
  };

  useEffect(() => () => cleanupBrowser(), []);

  const startBrowser = async () => {
    error.value = null;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      // live spectrum: vertical bars mirrored about y=0, x ~ frequency
      const ctx = new AudioContext();
      audioCtxRef.current = ctx;
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 1024;
      analyser.smoothingTimeConstant = 0.75;
      ctx.createMediaStreamSource(stream).connect(analyser);
      const freq = new Uint8Array(analyser.frequencyBinCount);
      // show up to ~6 kHz — where voice lives
      const bins = Math.min(
        analyser.frequencyBinCount,
        Math.ceil(6000 / (ctx.sampleRate / analyser.fftSize)),
      );
      const draw = () => {
        rafRef.current = window.requestAnimationFrame(draw);
        const canvas = canvasRef.current;
        if (!canvas) return;
        const dpr = window.devicePixelRatio || 1;
        const w = canvas.clientWidth;
        const h = canvas.clientHeight;
        if (canvas.width !== Math.round(w * dpr)) {
          canvas.width = Math.round(w * dpr);
          canvas.height = Math.round(h * dpr);
        }
        const g = canvas.getContext("2d");
        if (!g) return;
        g.setTransform(dpr, 0, 0, dpr, 0, 0);
        g.clearRect(0, 0, w, h);
        analyser.getByteFrequencyData(freq);
        const mid = h / 2;
        const bw = w / bins;
        for (let i = 0; i < bins; i++) {
          const v = freq[i] / 255;
          const bh = Math.max(0.75, v * (mid - 1));
          // indigo → violet across the frequency axis, hotter when louder
          const t = i / bins;
          g.fillStyle = `rgba(${99 + t * 69}, ${102 - t * 17}, ${241 + t * 6}, ${0.35 + v * 0.65})`;
          g.fillRect(i * bw + 0.5, mid - bh, Math.max(bw - 1, 1), bh * 2);
        }
        // y=0 axis hairline
        g.fillStyle = "rgba(165,180,252,0.35)";
        g.fillRect(0, mid - 0.5, w, 1);
      };
      draw();

      const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : undefined;
      const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
      recRef.current = rec;
      const chunks: Blob[] = [];
      rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
      rec.onstop = async () => {
        const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
        cleanupBrowser();
        if (phase.value !== "transcribing") return; // cancelled
        try {
          const res = await api.transcribe(blob);
          text.value = res.text;
          phase.value = res.text ? "review" : "failed";
          if (!res.text) error.value = "empty transcript";
        } catch (err) {
          error.value =
            err instanceof ApiRequestError && err.code === "transcode_unavailable"
              ? "server lacks ffmpeg — browser dictation unavailable (host mic still works)"
              : ((err as Error).message ?? "transcription failed");
          phase.value = "failed";
        }
      };
      rec.start();
      phase.value = "recording";
    } catch (err) {
      error.value = `mic access failed: ${(err as Error).message ?? err}`;
      phase.value = "failed";
    }
  };

  const startHost = async () => {
    error.value = null;
    partial.value = "";
    try {
      await api.dictation("start", { mode: "host" });
      phase.value = "recording";
    } catch (err) {
      error.value =
        err instanceof ApiRequestError && err.code === "mic_busy"
          ? "host mic is busy (ambient/listen holds the lease)"
          : ((err as Error).message ?? "failed to start");
      phase.value = "failed";
    }
  };

  const start = () => (mode.value === "browser" ? startBrowser() : startHost());

  const stop = async () => {
    if (mode.value === "browser") {
      phase.value = "transcribing";
      recRef.current?.stop();
    } else {
      phase.value = "transcribing";
      await api.dictation("stop").catch(() => {});
    }
  };

  const cancel = async () => {
    if (mode.value === "browser") {
      phase.value = "idle";
      recRef.current?.stop();
      cleanupBrowser();
    } else {
      await api.dictation("cancel").catch(() => {});
      phase.value = "idle";
    }
    partial.value = "";
  };

  const submit = async () => {
    if (!text.value.trim()) return;
    try {
      if (target.value === "prompt") {
        await api.prompt(text.value.trim());
        phase.value = "submitted";
      } else {
        const res = await api.answer(target.value, { text: text.value.trim() });
        if (res.status === "delivered" || res.status === "uncertain") {
          phase.value = "submitted";
        } else {
          error.value = `stale target: ${res.detail ?? res.status} — pick a target and retry`;
          phase.value = "failed";
        }
      }
      void refresh("deliveries");
    } catch (err) {
      error.value = (err as Error).message ?? "submit failed";
      phase.value = "failed";
    }
  };

  const pending = deliveries.value?.pending ?? [];

  return (
    <div
      style="position:fixed;inset:0;background:#05070dcc;backdrop-filter:blur(4px);z-index:50;
             display:grid;place-items:center;padding:16px"
      onClick={(e) => e.target === e.currentTarget && phase.value !== "recording" && onClose()}
    >
      <div class="card" style="padding:18px;max-width:560px;width:100%;display:flex;flex-direction:column;gap:12px">
        <div style="display:flex;align-items:center;gap:10px">
          <b>dictate</b>
          <span class="badge">{mode.value} mic</span>
          {canBrowserMic() && phase.value === "idle" && (
            <button
              class="btn small"
              onClick={() => (mode.value = mode.value === "browser" ? "host" : "browser")}
            >
              use {mode.value === "browser" ? "host" : "browser"} mic
            </button>
          )}
          <button class="btn small" style="margin-left:auto" onClick={onClose}>
            ✕
          </button>
        </div>

        <label style="display:flex;gap:8px;align-items:center;font-size:12px">
          <span style="color:var(--text-faint)">deliver to</span>
          <select
            class="input"
            style="flex:1"
            value={target.value}
            onChange={(e) => (target.value = (e.target as HTMLSelectElement).value)}
          >
            <option value="prompt">operator prompt → Mode A judgment (unbound)</option>
            {pending.map((p) => (
              <option key={p.event_id} value={p.event_id}>
                answer {p.session_id}/{p.pane_id} — {(p.question_text ?? "").slice(0, 60)}
              </option>
            ))}
          </select>
        </label>

        {(phase.value === "idle" || phase.value === "failed") && (
          <button class="btn primary" style="justify-content:center;padding:12px" onClick={start}>
            ◉ start recording
          </button>
        )}

        {phase.value === "recording" && (
          <div style="display:flex;flex-direction:column;gap:10px">
            <div style="display:flex;align-items:center;gap:10px">
              <span class="badge err">
                <span class="dot" style="background:var(--error)" /> recording
              </span>
              <span style="color:var(--text-faint);font-size:10px;margin-left:auto">
                {mode.value === "browser" ? "0–6 kHz" : "host mic"}
              </span>
            </div>
            {mode.value === "browser" && (
              <canvas
                ref={canvasRef}
                style="width:100%;height:72px;display:block;background:#05070d;
                       border:1px solid var(--line-soft);border-radius:8px"
              />
            )}
            {partial.value && (
              <div class="qtext" style="color:var(--text-dim)">
                {partial.value}
                <span style="color:var(--text-faint)"> …</span>
              </div>
            )}
            <div style="display:flex;gap:8px">
              <button class="btn primary" style="flex:1;justify-content:center" onClick={stop}>
                ⏹ stop &amp; transcribe
              </button>
              <button class="btn danger" onClick={cancel}>
                cancel
              </button>
            </div>
          </div>
        )}

        {phase.value === "transcribing" && (
          <div class="readout dim" style="justify-content:center;padding:14px">
            ⋯ transcribing
          </div>
        )}

        {(phase.value === "review" || phase.value === "submitted") && (
          <div style="display:flex;flex-direction:column;gap:8px">
            <textarea
              class="input"
              rows={3}
              style="resize:vertical"
              value={text.value}
              disabled={phase.value === "submitted"}
              onInput={(e) => (text.value = (e.target as HTMLTextAreaElement).value)}
            />
            {phase.value === "review" ? (
              <div style="display:flex;gap:8px">
                <button class="btn primary" style="flex:1;justify-content:center" onClick={submit}>
                  ⇧ submit
                </button>
                <button class="btn" onClick={start}>
                  ↺ re-record
                </button>
              </div>
            ) : (
              <div style="color:var(--ok);text-align:center">✓ submitted</div>
            )}
          </div>
        )}

        {error.value && phase.value === "failed" && (
          <div style="color:var(--error);font-size:12px">{error.value}</div>
        )}
        {!window.isSecureContext && (
          <div style="color:var(--text-faint);font-size:11px">
            browser mic needs HTTPS (tailscale serve) — using host mic; see docs/DASHBOARD.md
          </div>
        )}
      </div>
    </div>
  );
}
