/** Cursor adoption policy shared by the browser stream and its regression tests. */

export interface CursorEnvelope {
  type: "hello" | "event";
  cursor: string;
}

/** Hello is a handshake and never acknowledges an event on the client's behalf. */
export function cursorAfterEnvelope(
  current: string | null,
  envelope: CursorEnvelope,
): string | null {
  return envelope.type === "hello" ? current : envelope.cursor;
}
