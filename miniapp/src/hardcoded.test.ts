// An English sentence written straight into a screen cannot be translated, and nothing about the
// page says so: it simply stays English while everything around it turns Russian. This test reads
// the screens as text and fails on one — in a text node, or in a prop a person reads (`title`,
// `placeholder`, `aria-label`, `label`).
//
// What it deliberately does not flag: a single token with no space in it. Brand names (SearXNG,
// DuckDuckGo), tool ids (WebFetch), config fields (base_url), file names and paths are not words to
// translate, and a rule that caught them would be turned off within the week. Everything with two
// words in it is a sentence, and a sentence belongs in `i18n.ts`.

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";

const SOURCES = import.meta.glob("./**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true }) as Record<string, string>;

// Nothing is excused from this test. The voice modules were once on a list here, on the grounds that
// a reducer and two transports hold no words anybody reads; they held four — the chip on a waiting
// agent and three microphone failures — and they went on holding them in English on the Russian page
// for as long as the list said they could not. An exclusion that is not audited is a hiding place, so
// there is no list any more and a file that needs one has to earn it in `ALLOWED`, string by string.
const ELSEWHERE: string[] = [];

// Strings left in English on purpose, with the reason. Everything else has to come from the table.
const ALLOWED: Record<string, string[]> = {
  // The boundary between the app and the API: request paths and error text for a developer, never
  // prose on a page.
  "./api.ts": ["*"],
  // Placeholders that are examples of what to type, not words: an address, a path, a cron line.
  "./screens/Settings.tsx": [
    // A product's own name, as its own documentation writes it.
    "Serper (Google)",
    "http://host:9000/v1",
    "https://api.openai.com/v1",
    "socks5://127.0.0.1:1080",
    "google,duckduckgo,bing",
    "wt-wt, ru-ru, us-en",
    "ru, us",
    "ru, en",
  ],
  "./screens/Schedules.tsx": ["0 4 * * 1-5"],
  "./screens/AddModel.tsx": ["http://localhost:9000/v1"],
  "./modelselect.tsx": ["vllm/Qwen3.6"],
};

const PROPS = /\b(?:title|placeholder|aria-label|ariaLabel|label|alt)="([^"]{2,})"/g;
// A text node between tags: `>Some words<`, with no brace in it (a brace is an expression, and an
// expression is either a translation or a value). Read in `.tsx` files only, and never over a
// fragment that carries the punctuation of code — a type parameter and a comparison both sit
// between a `>` and a `<` without being anything a reader ever sees.
const TEXT = />([^<>{}\n]{3,})</g;
const CODEISH = /[=;`"|&$:]/;

/** Whether a string is prose: two or more words made of letters. */
function isProse(text: string): boolean {
  const words = text.trim().split(/\s+/).filter((w) => /[A-Za-z]{2,}/.test(w));
  return words.length >= 2;
}

function offenders(path: string, source: string): string[] {
  const allowed = ALLOWED[path] ?? [];
  if (allowed.includes("*")) return [];
  const found = new Set<string>();
  for (const re of [PROPS, TEXT]) {
    re.lastIndex = 0;
    if (re === TEXT && !path.endsWith(".tsx")) continue;
    for (const m of source.matchAll(re)) {
      const text = m[1].trim();
      if (!isProse(text) || allowed.includes(text)) continue;
      if (re === TEXT && CODEISH.test(text)) continue;
      found.add(text);
    }
  }
  return [...found];
}

describe("the screens", () => {
  it("say nothing in English of their own", () => {
    const left: string[] = [];
    for (const [path, source] of Object.entries(SOURCES)) {
      if (path.endsWith(".test.ts") || ELSEWHERE.includes(path)) continue;
      for (const text of offenders(path, source)) left.push(`${path}: ${text}`);
    }
    expect(left).toEqual([]);
  });

  it("looks at every screen the app has", () => {
    const screens = Object.keys(SOURCES).filter((p) => p.startsWith("./screens/"));
    const looked = screens.filter((p) => !ELSEWHERE.includes(p));
    expect(looked.length).toBeGreaterThanOrEqual(screens.length - ELSEWHERE.length);
    expect(looked).toContain("./screens/Settings.tsx");
    expect(looked).toContain("./screens/Session.tsx");
  });
});
