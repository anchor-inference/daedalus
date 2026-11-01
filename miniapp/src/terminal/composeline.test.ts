import { describe, expect, it } from "vitest";
import { DICT } from "../i18n";
import { composeMessage, foldsPaste, lineCount, PASTE_FOLD_CHARS, quoteOf, showsCompose, SLASH_COMMANDS, slashHints } from "./composeline";

describe("the compose line", () => {
  it("is drawn for a running staff member's terminal only", () => {
    expect(showsCompose({ owner: { kind: "staff", id: "st-ira" }, status: "running" })).toBe(true);
    expect(showsCompose({ owner: { kind: "staff", id: "st-ira" }, status: "exited" })).toBe(false);
    expect(showsCompose({ owner: { kind: "free", id: null }, status: "running" })).toBe(false);
    expect(showsCompose({ owner: { kind: "session", id: "s1" }, status: "running" })).toBe(false);
    expect(showsCompose(null)).toBe(false);
  });

  it("hints the CLI's own commands while one is being typed", () => {
    expect(slashHints("claude", "/co")).toEqual(["compact", "context", "cost"]);
    expect(slashHints("codex", "/")).toEqual([...SLASH_COMMANDS.codex]);
    // A finished name, a space after it, ordinary words, and a CLI with no list: no hints.
    expect(slashHints("claude", "/compact")).toBeNull();
    expect(slashHints("claude", "/compact now")).toBeNull();
    expect(slashHints("claude", "hello /co")).toBeNull();
    expect(slashHints("pi", "/")).toBeNull();
    expect(slashHints(null, "/")).toBeNull();
  });

  it("describes every command it hints, in both languages", () => {
    for (const name of new Set(Object.values(SLASH_COMMANDS).flat())) {
      const entry = DICT[`term.compose.cmd.${name}`];
      expect(entry?.en, name).toBeTruthy();
      expect(entry?.ru, name).toBeTruthy();
    }
  });

  it("folds a paste of more than one line, or a long one, into a chip", () => {
    expect(foldsPaste("one line")).toBe(false);
    expect(foldsPaste("a\nb")).toBe(true);
    expect(foldsPaste("x".repeat(PASTE_FOLD_CHARS + 1))).toBe(true);
    expect(lineCount("a\nb\nc\n")).toBe(3);
  });

  it("quotes a terminal selection line by line, and nothing for blank space", () => {
    expect(quoteOf("  $ npm test  \nFAIL cart.test.ts   \n\n")).toBe(">   $ npm test\n> FAIL cart.test.ts");
    expect(quoteOf("   \n  ")).toBe("");
    expect(quoteOf("x".repeat(2100)).endsWith("…")).toBe(true);
  });

  it("puts the quote, the words and the pastes together with a blank line between", () => {
    expect(composeMessage({ quote: "> FAIL", typed: "  why does this fail?  ", pastes: ["log line 1\nlog line 2\n"] })).toBe("> FAIL\n\nwhy does this fail?\n\nlog line 1\nlog line 2");
    expect(composeMessage({ quote: "", typed: "/compact", pastes: [] })).toBe("/compact");
    expect(composeMessage({ quote: "", typed: "   ", pastes: [] })).toBe("");
  });
});
