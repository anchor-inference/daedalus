import { describe, expect, it } from "vitest";
import { renderMarkdown, renderNote } from "../md";
import { tint } from "./highlight";
import {
  archivePath, continueList, crc32, excerpt, fileIndex, firstImage, indent, insertBlock, lineDiff, link, linePrefix, matches, noteMarkdown,
  ordered, reference, references, taskProgress, toggleTask, trigger, wordCount, wrap, zip, type Note, type NoteFile,
} from "./model";

const picture: NoteFile = { id: "abcdefghij12", scope: "chat", scope_id: "s1", name: "shot [1] (final).png", mime: "image/png", size: 2048, width: 640, height: 480, created_at: "", created_by: "operator" };
const pdf: NoteFile = { id: "zyxwvu987654", scope: "project", scope_id: "p1", name: "отчёт за май.pdf", mime: "application/pdf", size: 3 * 1024 * 1024, width: null, height: null, created_at: "", created_by: "agent" };
const files = fileIndex([picture], [pdf]);
const note = (over: Partial<Note>): Note => ({ id: "n", scope: "chat", scope_id: "s1", title: "", body: "", color: "", pinned: false, position: 0, version: 1, created_at: "2026-01-01", updated_at: "2026-01-01", updated_by: "operator", ...over });

describe("the reference form", () => {
  it("is plain Markdown the host writes the same way, whatever the file is called", () => {
    expect(reference(picture)).toBe("![shot \\[1\\] (final).png](files/abcdefghij12/shot%20%5B1%5D%20%28final%29.png)");
    expect(reference(pdf)).toBe("[отчёт за май.pdf](files/zyxwvu987654/отчёт%20за%20май.pdf)");
    expect(references(`a ${reference(picture)} b ${reference(pdf)} c ${reference(picture)} [x](files/short/x)`)).toEqual([picture.id, pdf.id]);
  });

  it("lands in an export at the very path the note names", () => {
    expect(archivePath(pdf)).toBe("files/zyxwvu987654/отчёт за май.pdf");
    expect(archivePath(picture)).toBe("files/abcdefghij12/shot [1] (final).png");
  });
});

describe("a note drawn", () => {
  it("shows a picture, a file chip, a missing file and a remote picture it does not fetch", () => {
    const html = renderNote(`${reference(picture)}\n\n${reference(pdf)} and [gone](files/aaaaaaaaaaaa/x.png)\n\n![tracker](https://example.com/p.gif)`, { file: (id) => files.get(id) ?? null });
    expect(html).toContain('data-note-image="abcdefghij12"');
    expect(html).toContain('aspect-ratio:640 / 480');
    expect(html).toContain('data-note-file="zyxwvu987654"');
    expect(html).toContain('data-kind="pdf"');
    expect(html).toContain("note-missing");
    expect(html).not.toContain('src="https://example.com');
    expect(html).toContain('class="note-remote"');
  });

  it("gives every heading an anchor, numbering a repeated one", () => {
    const html = renderNote("# Plan\n\n## Plan\n\n### Шаги и сроки", { file: () => null });
    expect(html).toContain('id="plan"');
    expect(html).toContain('id="plan-2"');
    expect(html).toContain('id="шаги-и-сроки"');
    expect(html).toContain('data-note-heading="plan"');
  });

  it("names on each box the source line it ticks, even past a fence and inside a quote", () => {
    const body = "intro\n- [ ] one\n```\n- [ ] not a task\n```\n> - [x] quoted\n  - [ ] nested";
    const html = renderNote(body, { file: () => null });
    const lines = [...html.matchAll(/data-task-line="(\d+)"/g)].map((m) => Number(m[1]));
    expect(lines).toEqual([1, 5, 6]);
    expect(toggleTask(body, 1)).toContain("- [x] one");
    expect(toggleTask(body, 5)).toContain("> - [ ] quoted");
    // A line that is no task is left alone.
    expect(toggleTask(body, 3)).toBe(body);
    expect(taskProgress(body)).toEqual({ done: 1, total: 3 });
  });

  it("leaves an answer exactly as it was", () => {
    // Outside renderNote the boxes stay drawings and a reference stays words, as answers always had them.
    const text = `- [ ] x\n\n# Head\n\n${reference(picture)}`;
    const html = renderMarkdown(text);
    expect(html).not.toContain("task-toggle");
    expect(html).not.toContain("note-img");
    expect(html).toContain("<h2>Head</h2>");
    expect(renderNote(text, { file: (id) => files.get(id) ?? null })).toContain("task-toggle");
  });
});

describe("a card", () => {
  it("reads a note as one line, keeping headings and items apart", () => {
    expect(excerpt("## Voice\n\nWarm and plain.\nStill plain.\n\n- one\n- two\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n```js\ncode()\n```")).toBe("Voice · Warm and plain. Still plain. · one · two · A, B · 1, 2");
    expect(excerpt("- [x] Menu\n- [ ] Photos")).toBe("Menu · Photos");
  });

  it("finds its first picture, its words and the names of its files", () => {
    expect(firstImage(`${reference(pdf)} ${reference(picture)}`, files)?.id).toBe(picture.id);
    expect(matches(note({ body: reference(pdf) }), "отчёт", files)).toBe(true);
    expect(matches(note({ title: "Plan", body: "flour" }), "plan flour", files)).toBe(true);
    expect(matches(note({ title: "Plan" }), "plan sugar", files)).toBe(false);
  });

  it("is listed pinned first, then in the operator's order", () => {
    const list = ordered([note({ id: "a", position: 0 }), note({ id: "b", position: -1 }), note({ id: "c", position: 5, pinned: true })]);
    expect(list.map((n) => n.id)).toEqual(["c", "b", "a"]);
  });

  it("counts words without the code or the addresses", () => {
    expect(wordCount("Two words\n```\nignored code here\n```\n[link text](https://example.com/a-b-c)")).toBe(4);
  });
});

describe("the editor's commands", () => {
  it("puts marks around a selection and takes them off again", () => {
    expect(wrap("say hi", 4, 6, "**")).toEqual({ text: "say **hi**", start: 6, end: 8 });
    expect(wrap("say **hi**", 6, 8, "**")).toEqual({ text: "say hi", start: 4, end: 6 });
    expect(wrap("", 0, 0, "_", "_", "text")).toEqual({ text: "_text_", start: 1, end: 5 });
  });

  it("makes a link with the address selected to type", () => {
    const made = link("see docs", 4, 8, "text");
    expect(made.text).toBe("see [docs](https://)");
    expect(made.text.slice(made.start, made.end)).toBe("https://");
    expect(link("https://a.b", 0, 11, "text").text).toBe("[text](https://a.b)");
  });

  it("turns lines into a list, numbered, and back", () => {
    expect(linePrefix("a\nb", 0, 3, "ol").text).toBe("1. a\n2. b");
    expect(linePrefix("- a\n- b", 0, 7, "ul").text).toBe("a\nb");
    expect(linePrefix("a", 1, 1, "task")).toEqual({ text: "- [ ] a", start: 7, end: 7 });
    expect(linePrefix("- a", 3, 3, "h2").text).toBe("## a");
  });

  it("goes on with a list on Enter and ends it on an empty item", () => {
    expect(continueList("- a", 3, 3)).toEqual({ text: "- a\n- ", start: 6, end: 6 });
    expect(continueList("3. a", 4, 4)?.text).toBe("3. a\n4. ");
    expect(continueList("- [x] done", 10, 10)?.text).toBe("- [x] done\n- [ ] ");
    expect(continueList("> quote", 7, 7)?.text).toBe("> quote\n> ");
    expect(continueList("x\n- ", 4, 4)).toEqual({ text: "x\n", start: 2, end: 2 });
    expect(continueList("plain", 5, 5)).toBeNull();
  });

  it("indents and outdents every touched line", () => {
    expect(indent("- a\n- b", 0, 7, false).text).toBe("  - a\n  - b");
    expect(indent("  - a", 4, 4, true)).toEqual({ text: "- a", start: 2, end: 2 });
  });

  it("puts a block on a line of its own", () => {
    expect(insertBlock("ab", 1, 1, "---")).toMatchObject({ text: "a\n\n---\n\nb" });
    expect(insertBlock("", 0, 0, "x", [0, 1])).toEqual({ text: "x\n", start: 0, end: 1 });
  });

  it("opens the file menu after [[ and the block menu after / at a line's start", () => {
    expect(trigger("see [[sho", 9)).toEqual({ kind: "file", from: 4, query: "sho" });
    expect(trigger("text\n  /tab", 11)).toEqual({ kind: "slash", from: 7, query: "tab" });
    expect(trigger("a / b", 3)).toBeNull();
  });
});

describe("the tinted source", () => {
  it("holds every letter of the source exactly once, in order", () => {
    const source = "# Title **b**\n- [x] done `c` [t](files/abcdefghij12/a.png)\n> q _e_\n| a | b |\n```\n<tag> & co\n```\n";
    const text = tint(source).replace(/<[^>]+>/g, "").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
    expect(text).toBe(source + "\n");
    expect(tint("[t](files/abcdefghij12/a.png)")).toContain('class="hl-ref"');
  });
});

describe("the export", () => {
  it("heads the Markdown with the title once", () => {
    expect(noteMarkdown({ title: "Plan", body: "words\n\n" })).toBe("# Plan\n\nwords\n");
    expect(noteMarkdown({ title: "Plan", body: "# Plan\nwords" })).toBe("# Plan\nwords\n");
  });

  it("writes a zip any unzip reads: signatures, sizes and checksums in place", async () => {
    const data = new TextEncoder().encode("hello");
    expect(crc32(data)).toBe(0x3610a686);
    const blob = zip([{ name: "a.md", data }, { name: "files/x/б.png", data: new Uint8Array([1, 2, 3]) }]);
    const bytes = new Uint8Array(await blob.arrayBuffer());
    const view = new DataView(bytes.buffer);
    expect(view.getUint32(0, true)).toBe(0x04034b50);
    expect(view.getUint32(14, true)).toBe(0x3610a686);
    const end = bytes.length - 22;
    expect(view.getUint32(end, true)).toBe(0x06054b50);
    expect(view.getUint16(end + 10, true)).toBe(2);
  });
});

describe("the conflict prompt", () => {
  it("lines the two versions up", () => {
    expect(lineDiff("a\nb\nc", "a\nB\nc")).toEqual([{ kind: "same", text: "a" }, { kind: "theirs", text: "b" }, { kind: "mine", text: "B" }, { kind: "same", text: "c" }]);
  });
});
