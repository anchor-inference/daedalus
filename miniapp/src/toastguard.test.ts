// The app's short line says what went wrong, or carries a button. It used to say that things had gone
// right as well, after nearly every press, and stood over a phone's composer doing it. A success shows
// in the thing that succeeded, so a new call of that kind is refused here.

import { describe, expect, it } from "vitest";

const SOURCES = import.meta.glob(["./**/*.ts", "./**/*.tsx", "!./**/*.test.*", "!./i18n.ts"], { eager: true, query: "?raw", import: "default" }) as Record<string, string>;
const CONFIRMATION = /\.(saved|added|removed|deleted|copied|done|created|sent|started|stopped|restored|connected|disconnected|cancelled|recorded)"/;

describe("the short line", () => {
  it("is not used to say that something worked", () => {
    const found: string[] = [];
    for (const [path, source] of Object.entries(SOURCES)) {
      source.split("\n").forEach((line, index) => {
        const call = /\b(?:toast|statusToast|showToast)\??\.?\(\s*t\("[^"]+"/.exec(line);
        // A call that carries Undo or a button is a control, whatever its words say.
        if (call && CONFIRMATION.test(call[0]) && !/undo:|action:|\{\s*$/.test(line)) found.push(`${path}:${index + 1}`);
      });
    }
    expect(found).toEqual([]);
  });
});
