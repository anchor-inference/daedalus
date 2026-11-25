// The browser must lay text out cell for cell as the terminal daemon's Ghostty does, or a restored
// screen drifts from the live one at the first emoji. The fixture is a set of grids recorded from
// libghostty-vt itself (grapheme clustering on): every pair of break classes, every triple of the
// classes that carry state, real-world sequences, the same sequences written one byte at a time, and
// clusters landing on the right margin. Here the same bytes go through @xterm/headless with the
// provider, and the grids must be identical — cluster boundaries and widths both. The one allowed
// difference is described at `sameCluster`.
//
// When Ghostty is bumped, the fixture is re-recorded from the new build and the table regenerated
// with `scripts/ghostty_widths.py`; a failure here after that is a real disagreement to settle, not a
// fixture to paper over.

import { Terminal } from "@xterm/headless";
import { describe, expect, it } from "vitest";
import fixture from "./testdata/ghostty-widths.json";
import { GHOSTTY_UNICODE_VERSION, ghosttyEntry, ghosttyUnicode, graphemeBreak } from "./unicode";

type Case = { name: string; cols: number; rows: number; writes: string[]; grid: string[][] };

function bytes(base64: string): Uint8Array {
  return Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
}

function terminal(cols: number, rows: number): Terminal {
  const term = new Terminal({ cols, rows, scrollback: 0, allowProposedApi: true });
  term.unicode.register(ghosttyUnicode);
  term.unicode.activeVersion = GHOSTTY_UNICODE_VERSION;
  return term;
}

async function feed(term: Terminal, chunks: (Uint8Array | string)[]): Promise<void> {
  for (const chunk of chunks) await new Promise<void>((resolve) => term.write(chunk, resolve));
}

/** The grid as the fixture writes it: per row, the widths of its clusters, then the clusters. */
function grid(term: Terminal): string[][] {
  const buffer = term.buffer.active;
  const out: string[][] = [];
  for (let y = 0; y < buffer.length; y++) {
    const line = buffer.getLine(y)!;
    const cells: [string, number][] = [];
    for (let x = 0; x < term.cols; x++) {
      const cell = line.getCell(x)!;
      const width = cell.getWidth();
      if (width === 0) continue;
      cells.push([cell.getChars() || " ", width]);
    }
    while (cells.length && cells[cells.length - 1][0] === " " && cells[cells.length - 1][1] === 1) cells.pop();
    out.push([cells.map(([, w]) => w).join(""), ...cells.map(([ch]) => ch)]);
  }
  while (out.length && out[out.length - 1].length === 1 && out[out.length - 1][0] === "") out.pop();
  return out;
}

const visible = (row: string[]) => row.slice(1).map((ch) => [...ch].map((c) => c.codePointAt(0)!.toString(16)).join("+")).join(" ");

/**
 * Whether an xterm.js cluster holds what Ghostty's does. Ghostty drops a zero-width code point that
 * would start a cluster (a U+200B, a stray variation selector); xterm.js has no way to drop one, so the
 * provider lets it ride invisibly in the cell on the left. The cell and its width are the same, which
 * is what keeps the rows aligned, so such extras are allowed — and only zero-width ones.
 */
function sameCluster(xterm: string, ghostty: string): boolean {
  const extra = [...xterm];
  const wanted = [...ghostty];
  let at = 0;
  for (const cp of extra) {
    if (at < wanted.length && cp === wanted[at]) at++;
    else if ((ghosttyEntry(cp.codePointAt(0)!) & 3) !== 0) return false;
  }
  return at === wanted.length;
}

function sameRow(xterm: string[], ghostty: string[]): boolean {
  if (xterm.length !== ghostty.length || xterm[0] !== ghostty[0]) return false;
  return xterm.every((cluster, i) => i === 0 || sameCluster(cluster, ghostty[i]));
}

describe("the Ghostty unicode provider", () => {
  for (const entry of (fixture as { cases: Case[] }).cases) {
    // Every write waits for the terminal's callback, one turn of the event loop each, and the bytewise
    // case writes over a thousand of them; a busy machine makes every turn long. The allowance grows
    // with the number of writes rather than being one number for all.
    it(`lays out "${entry.name}" as libghostty-vt does`, { timeout: 30_000 + 100 * entry.writes.length }, async () => {
      const term = terminal(entry.cols, entry.rows);
      await feed(term, entry.writes.map(bytes));
      const got = grid(term);
      const differences: string[] = [];
      for (let y = 0; y < Math.max(got.length, entry.grid.length); y++) {
        const a = got[y] ?? [""];
        const b = entry.grid[y] ?? [""];
        if (!sameRow(a, b)) differences.push(`row ${y}\n  xterm   ${a[0]} ${visible(a)}\n  ghostty ${b[0]} ${visible(b)}`);
      }
      expect(differences.slice(0, 5).join("\n")).toBe("");
      term.dispose();
    });
  }

  it("gives the newer emoji two cells, where xterm.js's own table gives one", () => {
    expect(ghosttyUnicode.wcwidth(0x1fae0)).toBe(2); // 🫠
    expect(ghosttyUnicode.wcwidth(0x1fa70)).toBe(2);
  });

  it("gives zero-width space no cell and ASCII one", () => {
    expect(ghosttyUnicode.wcwidth(0x200b)).toBe(0);
    expect(ghosttyUnicode.wcwidth(0x41)).toBe(1);
    expect(ghosttyUnicode.wcwidth(0x1b)).toBe(0);
  });

  it("reads code points past the last one Unicode has as one cell, like Ghostty", () => {
    expect(ghosttyEntry(0x110000) & 3).toBe(1);
  });

  // uucode's own cases for the Unicode 16+ Indic rule and its overlap with emoji sequences.
  const CLASS = (cp: number) => (ghosttyEntry(cp) >> 3) & 31;
  const cases: [number[], boolean[]][] = [
    [[0x094d, 0x0915], [false]],
    [[0x0061, 0x094d, 0x0915], [false, false]],
    [[0x094d, 0x0300, 0x200d, 0x0915], [false, false, false]],
    [[0x094d, 0x094d, 0x0915], [false, false]],
    [[0x0915, 0x0300, 0x0915], [false, true]],
    [[0x094d, 0x0915, 0x0300, 0x0915], [false, false, true]],
    [[0x094d, 0x200c, 0x0300, 0x0915], [false, false, true]],
    [[0x094d, 0x0903, 0x0915], [false, true]],
    [[0x094d, 0x1f3fb, 0x0915], [true, true]],
    [[0x0061, 0x1cf5, 0x0300, 0x0915], [true, false, false]],
    [[0x0061, 0x1cf6, 0x0915], [true, false]],
    [[0x1f600, 0x094d, 0x0300, 0x200d, 0x1f600], [false, false, false, false]],
    [[0x1f600, 0x094d, 0x0300, 0x0915], [false, false, false]],
    [[0x1f600, 0x094d, 0x200c, 0x200d, 0x1f600], [false, false, false, false]],
    [[0x1f600, 0x094d, 0x200c, 0x0915], [false, false, true]],
    [[0x1f44d, 0x1f3fb, 0x094d, 0x0915], [false, false, false]],
    [[0x1f600, 0x1cf5, 0x0915], [true, false]],
  ];
  for (const [cps, breaks] of cases) {
    it(`breaks ${cps.map((c) => c.toString(16)).join(" ")} where uucode does`, () => {
      let state = 0;
      const got: boolean[] = [];
      for (let i = 0; i + 1 < cps.length; i++) {
        const step = graphemeBreak(CLASS(cps[i]), CLASS(cps[i + 1]), state);
        got.push((step & 1) === 1);
        state = step >> 1;
      }
      expect(got).toEqual(breaks);
    });
  }

  // Where xterm.js cannot follow Ghostty, pinned so that a change in either is noticed.
  it("keeps a wide emoji wide under VS15, since xterm.js cannot narrow a cell it has written", async () => {
    const term = terminal(20, 3);
    await feed(term, ["|⌚︎|"]);
    expect(grid(term)[0][0]).toBe("121");
    term.dispose();
  });

  it("loses the soft hyphen, which xterm.js drops before any provider sees it", async () => {
    const term = terminal(20, 3);
    await feed(term, ["a­b"]);
    expect(grid(term)[0]).toEqual(["11", "a", "b"]);
    term.dispose();
  });

  it("attaches a mark after an escape sequence to the cell on the left, as Ghostty does", async () => {
    const term = terminal(20, 3);
    await feed(term, ["e\x1b[1ḿx"]);
    expect(grid(term)[0]).toEqual(["11", "é", "x"]);
    term.dispose();
  });
});
