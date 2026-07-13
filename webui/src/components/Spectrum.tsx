/** Always-available live voice spectrum (B087).

Expands while host mic is recording for STT (listen / ask / dictation) unless
the operator deliberately collapsed it during that recording period.
*/
import { useEffect, useRef } from "preact/hooks";
import {
  setSpectrumCollapsed,
  spectrumCollapsed,
  spectrumFrame,
} from "../lib/spectrum";

function drawBars(
  canvas: HTMLCanvasElement,
  bands: number[],
  recording: boolean,
): void {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (w <= 0 || h <= 0) return;
  const pw = Math.round(w * dpr);
  const ph = Math.round(h * dpr);
  if (canvas.width !== pw) canvas.width = pw;
  if (canvas.height !== ph) canvas.height = ph;
  const g = canvas.getContext("2d");
  if (!g) return;
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const n = Math.max(1, bands.length);
  const mid = h / 2;
  const bw = w / n;
  for (let i = 0; i < n; i++) {
    const v = Math.max(0, Math.min(1, bands[i] ?? 0));
    const bh = Math.max(recording ? 1.5 : 0.5, v * (mid - 2));
    const t = i / n;
    // indigo → violet; hotter when louder / recording
    const a = recording ? 0.4 + v * 0.6 : 0.18 + v * 0.35;
    g.fillStyle = `rgba(${99 + t * 69}, ${102 - t * 17}, ${241 + t * 6}, ${a})`;
    g.fillRect(i * bw + 0.4, mid - bh, Math.max(bw - 0.8, 1), bh * 2);
  }
  g.fillStyle = recording ? "rgba(165,180,252,0.45)" : "rgba(165,180,252,0.2)";
  g.fillRect(0, mid - 0.5, w, 1);
}

export function SpectrumPanel() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const frame = spectrumFrame.value;
  const collapsed = spectrumCollapsed.value;
  const recording = !!frame?.recording;
  const bands = frame?.bands ?? [];

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || collapsed) return;
    drawBars(canvas, bands, recording);
  }, [bands, recording, collapsed, frame?.ts]);

  // Keep painting at display refresh while recording even if frames stall briefly
  useEffect(() => {
    if (collapsed || !recording) return;
    let raf = 0;
    const tick = () => {
      const c = canvasRef.current;
      const f = spectrumFrame.value;
      if (c && f) drawBars(c, f.bands, f.recording);
      raf = window.requestAnimationFrame(tick);
    };
    raf = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(raf);
  }, [collapsed, recording]);

  const toggle = () => {
    setSpectrumCollapsed(!collapsed, { recording });
  };

  const live = recording || bands.some((b) => b > 0.02);
  const title = recording
    ? "mic live — recording for STT"
    : live
      ? "mic spectrum (ambient)"
      : "mic spectrum";

  return (
    <section class={`spectrum-panel ${recording ? "recording" : ""} ${collapsed ? "collapsed" : ""}`}>
      <button type="button" class="spectrum-head" onClick={toggle} title="toggle spectrum">
        <span class="spectrum-label">
          {recording ? (
            <span class="badge err" style="margin-right:8px">
              <span class="dot" style="background:var(--error)" /> rec
            </span>
          ) : (
            <span class={`spectrum-dot ${live ? "on" : ""}`} />
          )}
          {title}
          <span class="spectrum-meta">
            {frame?.max_hz ? `0–${Math.round(frame.max_hz / 1000)} kHz` : "0–6 kHz"}
            {frame?.source ? ` · ${frame.source}` : ""}
          </span>
        </span>
        <span class="spectrum-chevron">{collapsed ? "▸" : "▾"}</span>
      </button>
      {!collapsed && (
        <canvas
          ref={canvasRef}
          class="spectrum-canvas"
          aria-label="live voice frequency spectrum"
        />
      )}
    </section>
  );
}
