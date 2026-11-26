// The browser's own list, confirmation and alert cannot be styled, do not follow the theme, and inside
// the Telegram webview the dialogs are refused outright. Every screen asks through the primitives
// instead (`Select`, `Combobox`, `confirmDialog`), and this test reads the sources as text so that a
// native one does not come back unnoticed. Date, time, number, range and colour inputs stay native:
// the browser's pickers for them are the control, and there is nothing of ours to replace them with.

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";

const SOURCES = import.meta.glob(["./**/*.{ts,tsx}", "!./**/*.test.{ts,tsx}"], { query: "?raw", import: "default", eager: true }) as Record<string, string>;

/** The places in the sources where a pattern occurs, as `file:line`. */
function found(pattern: RegExp): string[] {
  const out: string[] = [];
  for (const [file, text] of Object.entries(SOURCES)) {
    text.split("\n").forEach((line, i) => {
      if (pattern.test(line)) out.push(`${file}:${i + 1}`);
    });
  }
  return out;
}

describe("native controls", () => {
  it("never draws the browser's select", () => {
    expect(found(/<select[\s>]/)).toEqual([]);
  });

  it("never asks through the browser's confirm, alert or prompt", () => {
    expect(found(/\bwindow\.(confirm|alert|prompt)\s*\(/)).toEqual([]);
  });
});
