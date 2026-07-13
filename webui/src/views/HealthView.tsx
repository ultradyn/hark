import { useSignal } from "@preact/signals";
import { useEffect } from "preact/hooks";
import { api } from "../lib/api";
import { health, usePoll } from "../lib/data";
import type { ConfigResponse } from "../lib/types";

function Check({ ok, label, detail }: { ok: boolean; label: string; detail?: string | null }) {
  return (
    <div style="display:flex;gap:10px;align-items:baseline">
      <span style={`color:${ok ? "var(--ok)" : "var(--error)"};min-width:16px`}>
        {ok ? "✓" : "✗"}
      </span>
      <span>{label}</span>
      {detail && <span style="color:var(--text-faint);font-size:11px">{detail}</span>}
    </div>
  );
}

export function HealthView() {
  usePoll("health", 8000);
  const cfg = useSignal<ConfigResponse | null>(null);
  useEffect(() => {
    void api.config().then((c) => (cfg.value = c));
  }, []);

  const h = health.value;
  const doctor = h?.doctor as
    | (Record<string, unknown> & {
        ok: boolean;
        herdr_ok?: boolean;
        speech_ok?: boolean;
        sessions?: { session_id: string; ok: boolean; version?: string; error?: string | null }[];
        providers?: { name: string; available: boolean; source?: string | null; detail?: string | null }[];
      })
    | undefined;

  return (
    <div class="panelwrap">
      <section class="section">
        <h3 class="paneltitle">server</h3>
        <div class="readout" style="gap:14px">
          {h ? (
            <>
              <span class="badge live">
                <span class="dot" /> {h.server.name} {h.server.version}
              </span>
              <span style="color:var(--text-dim)">{h.server.bind}</span>
              <span class="badge">{h.server.auth_required ? "auth on" : "auth off (localhost)"}</span>
              <span class="badge">{h.server.tls_terminated ? "tls terminated" : "plain http"}</span>
              <span class="badge">{h.server.ffmpeg ? "ffmpeg ✓" : "ffmpeg ✗ (browser mic off)"}</span>
              <span style="margin-left:auto;color:var(--text-faint);font-size:11px">
                up since {new Date(h.server.started_at).toLocaleTimeString(undefined, { hour12: false })}
              </span>
            </>
          ) : (
            <span class="readout dim">loading…</span>
          )}
        </div>
      </section>
      <section class="section">
        <h3 class="paneltitle">doctor</h3>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:2px 24px">
          {doctor ? (
            <>
              <Check ok={!!doctor.ok} label="overall" />
              <Check ok={doctor.herdr_ok !== false} label="herdr" />
              <Check ok={doctor.speech_ok !== false} label="speech providers" />
              {doctor.sessions?.map((s) => (
                <Check
                  key={s.session_id}
                  ok={s.ok}
                  label={`session ${s.session_id}`}
                  detail={s.ok ? `herdr ${s.version ?? ""}` : (s.error ?? "unreachable")}
                />
              ))}
              {doctor.providers?.map((p) => (
                <Check
                  key={p.name}
                  ok={p.available}
                  label={`provider ${p.name}`}
                  detail={p.source ?? p.detail ?? undefined}
                />
              ))}
            </>
          ) : (
            <span class="readout dim">loading…</span>
          )}
        </div>
      </section>
      <section class="section">
        <h3 class="paneltitle">config (redacted)</h3>
        <div>
          <details open>
            <summary style="cursor:pointer;color:var(--text-dim);padding:8px 0">
              {String(cfg.value?.config?.path ?? "config.toml")}
            </summary>
            <pre style="margin:0 0 12px;overflow:auto;font-size:11px;color:#9fb0d6;max-height:50vh">
              {cfg.value ? JSON.stringify(cfg.value.config, null, 2) : "loading…"}
            </pre>
          </details>
        </div>
      </section>
    </div>
  );
}
