/** Web Notifications for agent.blocked / needs_input (secure contexts only). */

import { signal } from "@preact/signals";
import { onEnvelope } from "./stream";
import type { HepPayload } from "./types";

export const notifyEnabled = signal(false);
export const notifySupported = window.isSecureContext && "Notification" in window;
/** Set when a notification is clicked — views scroll to this event. */
export const focusEventId = signal<string | null>(null);

const LS_KEY = "hark.notify";

export async function toggleNotifications(): Promise<void> {
  if (!notifySupported) return;
  if (notifyEnabled.value) {
    notifyEnabled.value = false;
    localStorage.setItem(LS_KEY, "0");
    return;
  }
  const perm = await Notification.requestPermission();
  notifyEnabled.value = perm === "granted";
  localStorage.setItem(LS_KEY, notifyEnabled.value ? "1" : "0");
}

export function wireNotifications(): void {
  if (!notifySupported) return;
  if (localStorage.getItem(LS_KEY) === "1" && Notification.permission === "granted") {
    notifyEnabled.value = true;
  }
  navigator.serviceWorker?.addEventListener("message", (e) => {
    if (e.data?.kind === "notification-click" && e.data.eventId) {
      focusEventId.value = e.data.eventId;
    }
  });
  onEnvelope((env) => {
    if (!notifyEnabled.value || document.visibilityState === "visible") return;
    if (env.source !== "watch" && env.source !== "ambient") return;
    const p = env.payload as HepPayload;
    if (p.kind !== "agent.blocked" && p.kind !== "agent.needs_input") return;
    const target = p.target?.friendly_name ?? p.target?.pane_id ?? "an agent";
    const n = new Notification(`hark: ${target} needs input`, {
      body: p.question?.text ?? "an agent is waiting for your answer",
      icon: "/icon-192.png",
      tag: p.event_id, // dedupe repeats of the same ask
      data: { eventId: p.event_id },
    });
    n.onclick = () => {
      window.focus();
      if (p.event_id) focusEventId.value = p.event_id;
      n.close();
    };
  });
}
