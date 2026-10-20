// An English sentence written straight into the side panel cannot be translated, and nothing says so: it
// stays English while everything around it turns Russian. These tests read the extension's sources
// as text and fail on one, and prove both dictionaries carry every key with the same placeholders.
//
// A single token with no space in it is not flagged: a product name, a method name, an address. Two
// words or more are a sentence, and a sentence belongs in `src/i18n.ts`.

import { readFileSync, readdirSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { DICTIONARIES, isKey } from "../src/i18n.js";

const src = new URL("../src/", import.meta.url);
const read = (name: string) => readFileSync(new URL(name, src), "utf8");
const panelHtml = readFileSync(new URL("../static/panel.html", import.meta.url), "utf8");

// Files whose strings are never shown to a person, with the reason. Everything else may hold no
// prose outside the dictionary.
const ALLOWED: Record<string, string> = {
  // The dictionary itself.
  "i18n.ts": "*",
  // Refusal reasons go to the daemon, which logs them and answers the agent; the operator never
  // reads them in the extension.
  "allowlist.ts": "*",
  // Error messages in `result` frames, read by the daemon the same way.
  "relay.ts": "result",
  // Chrome's own native-messaging error text, matched to tell its failures apart; never shown.
  "transport.ts": "*",
  // The bridge's error codes go to the framed app, which shows its own words for each.
  "bridge.ts": "*",
};

// A fragment that carries the punctuation of code (CSS, a template built of translations) is not
// something a reader sees as written.
const CODEISH = /[=;|&{}$]|:\S/;

/** Whether a string is prose: two or more words made of letters. */
function isProse(text: string): boolean {
  return text.trim().split(/\s+/).filter((word) => /[A-Za-zА-Яа-я]{2,}/.test(word)).length >= 2;
}

/** String literals in a TypeScript source, with comments removed first. */
function literals(source: string): { text: string; line: string }[] {
  const lines = source.replace(/\/\*[\s\S]*?\*\//g, "").split("\n").map((line) => line.replace(/^\s*\/\/.*$/, ""));
  const found: { text: string; line: string }[] = [];
  for (const line of lines) {
    for (const match of line.matchAll(/"((?:[^"\\]|\\.)*)"|`((?:[^`\\]|\\.)*)`|'((?:[^'\\]|\\.)*)'/g)) {
      found.push({ text: match[1] ?? match[2] ?? match[3] ?? "", line });
    }
  }
  return found;
}

describe("the dictionaries", () => {
  it("carry the same keys in Russian and English, none empty", () => {
    expect(Object.keys(DICTIONARIES.ru).sort()).toEqual(Object.keys(DICTIONARIES.en).sort());
    for (const dictionary of Object.values(DICTIONARIES)) {
      for (const [key, text] of Object.entries(dictionary)) expect(text.trim(), key).not.toBe("");
    }
  });

  it("use the same placeholders in both languages", () => {
    const placeholders = (text: string) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
    for (const key of Object.keys(DICTIONARIES.en) as (keyof typeof DICTIONARIES.en)[]) {
      expect(placeholders(DICTIONARIES.ru[key]), key).toEqual(placeholders(DICTIONARIES.en[key]));
    }
  });

  it("are translated, not copied", () => {
    // A template made of the product's name and placeholders reads the same in both languages.
    const words = (text: string) => text.replace(/\{\w+\}|Daedalus/g, "");
    const same = Object.keys(DICTIONARIES.en).filter(
      (key) => isProse(words(DICTIONARIES.en[key as keyof typeof DICTIONARIES.en])) &&
        DICTIONARIES.en[key as keyof typeof DICTIONARIES.en] === DICTIONARIES.ru[key as keyof typeof DICTIONARIES.ru],
    );
    expect(same).toEqual([]);
  });
});

describe("no hard-coded words", () => {
  it("the panel page holds structure only, every text by key", () => {
    const text = [...panelHtml.replace(/<script[\s\S]*?<\/script>/g, "").matchAll(/>([^<>]+)</g)]
      .map((m) => m[1].trim())
      .filter(Boolean);
    expect(text).toEqual([]);
    const keys = [...panelHtml.matchAll(/data-i18n(?:-title)?="([^"]+)"/g)].map((m) => m[1]);
    expect(keys.length).toBeGreaterThan(10);
    expect(keys.filter((key) => !isKey(key))).toEqual([]);
    const props = [...panelHtml.matchAll(/\b(?:title|placeholder|aria-label|alt)="([^"]*)"/g)].map((m) => m[1]);
    expect(props.filter(isProse)).toEqual([]);
  });

  const files = readdirSync(src).filter((name) => name.endsWith(".ts") && !name.endsWith(".d.ts"));
  for (const name of files) {
    it(`${name} shows no sentence of its own`, () => {
      const allowed = ALLOWED[name];
      if (allowed === "*") return;
      const offenders = literals(read(name)).filter(({ text, line }) => {
        if (!isProse(text) || CODEISH.test(text)) return false;
        // Error text inside a `result` frame or a thrown call error goes to the daemon.
        if (allowed === "result" && /refuse\(|CallError\(|message:/.test(line)) return false;
        return true;
      });
      expect(offenders.map((o) => o.text)).toEqual([]);
    });
  }

  it("every key the code asks for exists", () => {
    for (const name of files) {
      for (const match of read(name).matchAll(/\bt\("([^"]+)"/g)) expect(isKey(match[1]), `${name}: ${match[1]}`).toBe(true);
    }
  });
});
