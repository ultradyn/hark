import assert from "node:assert/strict";
import test from "node:test";

import { cursorAfterEnvelope } from "../src/lib/stream_cursor.ts";

test("hello does not advance the browser replay cursor", () => {
  assert.equal(
    cursorAfterEnvelope("watch:1", { type: "hello", cursor: "watch:9" }),
    "watch:1",
  );
  assert.equal(cursorAfterEnvelope(null, { type: "hello", cursor: "watch:9" }), null);
});

test("delivered events advance the browser replay cursor", () => {
  assert.equal(
    cursorAfterEnvelope("watch:1", { type: "event", cursor: "watch:2" }),
    "watch:2",
  );
});
