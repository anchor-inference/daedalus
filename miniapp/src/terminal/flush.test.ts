// @vitest-environment jsdom
// A resize while xterm.js is part-way through parsing a flood must not parse any output twice. These
// write numbered lines in chunks the size of the daemon's batches, resize while the parse loop is
// sliced, and count both the lines on screen and the write callbacks (each one an acknowledgement to
// the daemon). Stock xterm.js repeats what it had already parsed; the same runs without the fix say so.

import { Terminal as Headless } from "@xterm/headless";
import { Terminal as Browser } from "@xterm/xterm";
import { describe, expect, it } from "vitest";
import { flushOnlyUnparsed } from "./flush";

const LINES_PER_CHUNK = 900;
// Each test parses this many chunks of about 64 KiB, several megabytes through a real parser: the
// work is the test, and it is slower for as long as the machine is busy. Enough chunks that the
// resize lands inside a sliced parse many times; no more than that.
const CHUNKS = 20;
// Generous for that work, not for a hang: a stuck flush is told by its callbacks, not by this limit.
const PARSE_TIMEOUT = 60_000;

function chunk(c: number): string {
  let s = "";
  for (let l = 0; l < LINES_PER_CHUNK; l++) s += `${String(c * LINES_PER_CHUNK + l).padStart(8, "0")} ${"x".repeat(60)}\r\n`;
  return s; // about 64 KiB, one daemon batch
}

function terminal(fixed: boolean): Headless {
  const term = new Headless({ cols: 80, rows: 24, scrollback: 200_000, allowProposedApi: true });
  if (fixed) expect(flushOnlyUnparsed(term).installed).toBe(true);
  return term;
}

/** The numbered lines on screen and in the scrollback, and how often the sequence broke. */
function numbered(term: Headless): { count: number; breaks: number } {
  const buffer = term.buffer.active;
  const nums: number[] = [];
  for (let y = 0; y < buffer.length; y++) {
    const m = /^(\d{8}) /.exec(buffer.getLine(y)!.translateToString(true));
    if (m) nums.push(Number(m[1]));
  }
  let breaks = 0;
  for (let i = 1; i < nums.length; i++) if (nums[i] !== nums[i - 1] + 1) breaks++;
  return { count: nums.length, breaks };
}

/** Writes `chunks` chunks and resizes on every turn of the event loop until they are parsed. */
async function floodWhileResizing(term: Headless, chunks: number): Promise<number> {
  let callbacks = 0;
  let parsed = 0;
  await new Promise<void>((resolve) => {
    for (let c = 0; c < chunks; c++) {
      term.write(chunk(c), () => {
        callbacks++;
        if (++parsed === chunks) resolve();
      });
    }
    const resize = () => {
      if (parsed >= chunks) return;
      term.resize(term.cols === 80 ? 100 : 80, 24);
      setTimeout(resize, 0);
    };
    setTimeout(resize, 0);
  });
  // Anything a doubled flush would still call arrives by now.
  await new Promise((resolve) => setTimeout(resolve, 20));
  return callbacks;
}

describe("a resize during a sliced parse", () => {
  it("parses every chunk once and calls its callback once", { timeout: PARSE_TIMEOUT }, async () => {
    const term = terminal(true);
    const chunks = CHUNKS;
    expect(await floodWhileResizing(term, chunks)).toBe(chunks);
    expect(numbered(term)).toEqual({ count: chunks * LINES_PER_CHUNK, breaks: 0 });
    term.dispose();
  });

  it("is what stock xterm.js gets wrong, so the test has teeth", { timeout: PARSE_TIMEOUT }, async () => {
    const term = terminal(false);
    const chunks = CHUNKS;
    const callbacks = await floodWhileResizing(term, chunks);
    const lines = numbered(term);
    expect(callbacks).toBeGreaterThan(chunks);
    expect(lines.count).toBeGreaterThan(chunks * LINES_PER_CHUNK);
    term.dispose();
  });
});

describe("a resize from inside a write callback", () => {
  async function resizeInCallback(fixed: boolean): Promise<{ callbacks: number; lines: { count: number; breaks: number } }> {
    const term = terminal(fixed);
    let callbacks = 0;
    await new Promise<void>((resolve) => {
      term.write(chunk(0), () => {
        callbacks++;
        term.resize(100, 30);
      });
      term.write(chunk(1), () => callbacks++);
      term.write(chunk(2), () => {
        callbacks++;
        resolve();
      });
    });
    await new Promise((resolve) => setTimeout(resolve, 20));
    const lines = numbered(term);
    term.dispose();
    return { callbacks, lines };
  }

  it("does not parse the chunk whose callback asked for it again", { timeout: PARSE_TIMEOUT }, async () => {
    expect(await resizeInCallback(true)).toEqual({ callbacks: 3, lines: { count: 3 * LINES_PER_CHUNK, breaks: 0 } });
  });

  it("which stock xterm.js does", { timeout: PARSE_TIMEOUT }, async () => {
    const stock = await resizeInCallback(false);
    expect(stock.callbacks).toBeGreaterThan(3);
  });
});

describe("the browser build the app ships", () => {
  it("has the internals the fix needs", () => {
    const term = new Browser({ cols: 80, rows: 24, allowProposedApi: true });
    const fix = flushOnlyUnparsed(term);
    expect(fix.installed).toBe(true);
    fix.dispose();
    term.dispose();
  });

  it("stays out of the way on a build without them", () => {
    expect(flushOnlyUnparsed({ _core: {} }).installed).toBe(false);
    expect(flushOnlyUnparsed(null).installed).toBe(false);
  });
});
