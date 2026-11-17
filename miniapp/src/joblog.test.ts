// A job's log as the Jobs tab follows it: pieces joined into screen lines, a cap on what is kept, and
// when to go on asking.

import { describe, expect, it } from "vitest";
import type { JobLogPiece } from "./api";
import { EMPTY_JOB_LOG, JOB_LOG_BEHIND, behind, nextOffset, screenLine, shownLines, stillFollowing, takePiece } from "./joblog";

function piece(over: Partial<JobLogPiece>): JobLogPiece {
  return { id: "job-1", state: "running", outcome: null, exit_code: null, running: true, text: "", start: 0, next: 0, size: 0, restart: false, lines_only: false, ...over };
}

describe("a job's log", () => {
  it("leaves each line as a terminal would: escapes gone, a redrawn line at its last drawing", () => {
    expect(screenLine("\x1b[32mok\x1b[0m")).toBe("ok");
    expect(screenLine("10%\r50%\r100%")).toBe("100%");
    expect(screenLine("done\r")).toBe("done");
    expect(screenLine("\x1b]0;title\x07plain")).toBe("plain");
  });

  it("joins pieces cut inside a line, and draws the line being written as it stands", () => {
    let log = takePiece(EMPTY_JOB_LOG, piece({ text: "one\ntw", next: 6, size: 6, restart: true }));
    expect(log.lines).toEqual(["one"]);
    expect(shownLines(log)).toEqual(["one", "tw"]);
    log = takePiece(log, piece({ text: "o\nthree\n", start: 6, next: 14, size: 14 }));
    expect(shownLines(log)).toEqual(["one", "two", "three"]);
    expect(log.next).toBe(14);
  });

  it("follows a progress bar across pieces to its last drawing", () => {
    let log = takePiece(EMPTY_JOB_LOG, piece({ text: "build 10%", next: 9, size: 9, restart: true }));
    log = takePiece(log, piece({ text: "\rbuild 90%", start: 9, next: 19, size: 19 }));
    expect(shownLines(log)).toEqual(["build 90%"]);
    log = takePiece(log, piece({ text: "\rbuild 100%\n", start: 19, next: 31, size: 31 }));
    expect(log.lines).toEqual(["build 100%"]);
  });

  it("starts a tail read at the first whole line and says the start was cut", () => {
    const log = takePiece(EMPTY_JOB_LOG, piece({ text: "lf line\nwhole\n", start: 900, next: 914, size: 914, restart: true }));
    expect(log.lines).toEqual(["whole"]);
    expect(log.cut).toBe(true);
  });

  it("keeps only the newest lines and counts the ones it let go", () => {
    let log = takePiece(EMPTY_JOB_LOG, piece({ text: "a\nb\nc\n", next: 6, size: 6, restart: true }), 2);
    expect(log.lines).toEqual(["b", "c"]);
    log = takePiece(log, piece({ text: "d\n", start: 6, next: 8, size: 8 }), 2);
    expect(log.lines).toEqual(["c", "d"]);
    expect(log.dropped).toBe(2);
  });

  it("drops what it holds when the host reads the log afresh", () => {
    const held = takePiece(EMPTY_JOB_LOG, piece({ text: "old\n", next: 4, size: 4, restart: true }));
    const fresh = takePiece(held, piece({ text: "new\n", next: 4, size: 4, restart: true }));
    expect(fresh.lines).toEqual(["new"]);
  });

  it("replaces the view on every read from a machine that gives only its last lines", () => {
    const log = takePiece(takePiece(EMPTY_JOB_LOG, piece({ text: "a\nb", lines_only: true, size: -1 })), piece({ text: "b\nc", lines_only: true, size: -1 }));
    expect(log.lines).toEqual(["b", "c"]);
    expect(nextOffset(piece({ lines_only: true, size: -1 }))).toBe(-1);
  });

  it("goes on while the job runs or bytes are left, and skips to the tail when far behind", () => {
    expect(stillFollowing(piece({ running: true, next: 5, size: 5 }))).toBe(true);
    expect(stillFollowing(piece({ running: false, next: 5, size: 9 }))).toBe(true);
    expect(stillFollowing(piece({ running: false, next: 9, size: 9 }))).toBe(false);
    expect(behind(piece({ next: 5, size: 9 }))).toBe(true);
    expect(nextOffset(piece({ next: 5, size: 9 }))).toBe(5);
    expect(nextOffset(piece({ next: 5, size: 5 + JOB_LOG_BEHIND + 1 }))).toBe(-1);
  });
});
