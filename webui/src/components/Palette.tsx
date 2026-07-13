import { useComputed, useSignal } from "@preact/signals";
import { useEffect, useRef } from "preact/hooks";
import type { ViewId } from "../app";
import { deliveries, sessions } from "../lib/data";

export interface Command {
  id: string;
  label: string;
  hint?: string;
  run: () => void;
}

/** ⌘K / ctrl-K command palette: views, panes, pending answers, actions. */
export function Palette({
  onView,
  onDictate,
  onPane,
  onClose,
}: {
  onView: (v: ViewId) => void;
  onDictate: () => void;
  onPane: (session: string, pane: string) => void;
  onClose: () => void;
}) {
  const query = useSignal("");
  const cursor = useSignal(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => inputRef.current?.focus(), []);

  const commands = useComputed<Command[]>(() => {
    const cmds: Command[] = [
      { id: "v:events", label: "view: events", run: () => onView("events") },
      { id: "v:herdr", label: "view: herdr", run: () => onView("herdr") },
      { id: "v:queue", label: "view: queue", run: () => onView("queue") },
      { id: "v:voice", label: "view: voice", run: () => onView("voice") },
      { id: "v:health", label: "view: health", run: () => onView("health") },
      { id: "a:dictate", label: "dictate — voice prompt / answer", hint: "◉", run: onDictate },
    ];
    for (const s of sessions.value?.sessions ?? []) {
      for (const a of s.agents) {
        cmds.push({
          id: `p:${s.session_id}/${a.pane_id}`,
          label: `pane: ${s.session_id}/${a.pane_id} ${a.agent ?? ""} ${a.friendly_name ?? ""}`,
          hint: a.status,
          run: () => onPane(s.session_id, a.pane_id),
        });
      }
    }
    for (const p of deliveries.value?.pending ?? []) {
      cmds.push({
        id: `q:${p.event_id}`,
        label: `answer: ${p.session_id}/${p.pane_id} — ${(p.question_text ?? "").slice(0, 50)}`,
        hint: p.risk ?? "",
        run: () => onView("queue"),
      });
    }
    const q = query.value.toLowerCase().trim();
    if (!q) return cmds;
    // simple subsequence fuzzy match
    return cmds.filter((c) => {
      const hay = c.label.toLowerCase();
      let i = 0;
      for (const ch of q) {
        i = hay.indexOf(ch, i);
        if (i < 0) return false;
        i += 1;
      }
      return true;
    });
  });

  const pick = (i: number) => {
    const cmd = commands.value[i];
    if (cmd) {
      cmd.run();
      onClose();
    }
  };

  return (
    <div
      style="position:fixed;inset:0;background:#05070daa;z-index:60;display:flex;
             justify-content:center;padding-top:12vh"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div class="card" style="width:min(560px,92vw);height:fit-content;max-height:60vh;
                               display:flex;flex-direction:column;overflow:hidden">
        <input
          ref={inputRef}
          class="input"
          style="border:none;border-bottom:1px solid var(--line);border-radius:0;padding:12px 16px"
          placeholder="jump to view, pane, pending answer…"
          value={query.value}
          onInput={(e) => {
            query.value = (e.target as HTMLInputElement).value;
            cursor.value = 0;
          }}
          onKeyDown={(e) => {
            if (e.key === "Escape") onClose();
            else if (e.key === "ArrowDown") {
              e.preventDefault();
              cursor.value = Math.min(cursor.value + 1, commands.value.length - 1);
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              cursor.value = Math.max(cursor.value - 1, 0);
            } else if (e.key === "Enter") pick(cursor.value);
          }}
        />
        <div style="overflow-y:auto">
          {commands.value.slice(0, 12).map((c, i) => (
            <button
              key={c.id}
              class="navitem"
              style={`width:100%;border-radius:0;${i === cursor.value ? "background:#6366f11a;color:var(--text)" : ""}`}
              onMouseEnter={() => (cursor.value = i)}
              onClick={() => pick(i)}
            >
              {c.label}
              {c.hint && <span class="count">{c.hint}</span>}
            </button>
          ))}
          {commands.value.length === 0 && (
            <div style="padding:14px;color:var(--text-faint)">no matches</div>
          )}
        </div>
      </div>
    </div>
  );
}
