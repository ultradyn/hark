/** Unicode sparklines + relative time — dense, chart-lib-free. */

const BARS = "▁▂▃▄▅▆▇█";

export function sparkline(values: number[], width = 24): string {
  if (values.length === 0) return "";
  const vs = values.slice(-width);
  const max = Math.max(...vs);
  const min = Math.min(...vs);
  const span = max - min || 1;
  return vs.map((v) => BARS[Math.round(((v - min) / span) * (BARS.length - 1))]).join("");
}

export function relTime(tsMs: number): string {
  const s = Math.max(0, (Date.now() - tsMs) / 1000);
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h`;
  return `${(s / 86400).toFixed(1)}d`;
}

export function pct(x: number | undefined | null): string {
  return `${((x ?? 0) * 100).toFixed(1)}%`;
}
