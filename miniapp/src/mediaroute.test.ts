// @vitest-environment jsdom
// Pictures and clips in the conversation open in the centred viewer, paging through the rest of the
// conversation's media; documents and cited lines still open in the files panel.
import { describe, expect, it } from "vitest";
import { conversationMedia, galleryFor, toolSource, viewerKind, viewerSource } from "./mediaroute";
import type { ToolItem, Turn } from "./turns";

const tool = (id: string, name: string, path: string, extra: Partial<ToolItem> = {}): ToolItem => ({ kind: "tool", id, name, args: { path }, running: false, ...extra });
const turn = (key: string, activity: ToolItem[]): Turn => ({ key, activity, answer: "", startedAt: 0, endedAt: 0 }) as Turn;

describe("where a click in the conversation goes", () => {
  it("sends pictures and clips to the viewer and documents to the panel", () => {
    expect(viewerKind("shot.PNG")).toBe("image");
    expect(viewerKind("clip.webm")).toBe("video");
    expect(viewerKind("report.pdf")).toBeNull();
    expect(viewerKind("notes.md")).toBeNull();
    expect(viewerSource({ base: "/api/sessions/s", path: "a/shot.png" })).toEqual({ base: "/api/sessions/s", path: "a/shot.png" });
    expect(viewerSource({ base: "/api/sessions/s", path: "main.py" })).toBeNull();
    // A citation names lines: that is a source file opened at a place, never a picture.
    expect(viewerSource({ base: "/api/sessions/s", path: "shot.png", lines: "3" })).toBeNull();
    expect(viewerSource({ file: new File(["x"], "pasted.png") })).toBeNull();
  });

  it("finds a step's file in the workspace, an attached folder and a Windows workspace", () => {
    const folders = [{ id: "docs", path: "/srv/docs" }];
    expect(toolSource(tool("1", "ImageView", "/work/out/a.png"), "s", "/work", folders)).toEqual({ base: "/api/sessions/s", path: "out/a.png" });
    expect(toolSource(tool("1", "ImageView", "/srv/docs/b.png"), "s", "/work", folders)).toEqual({ base: "/api/sessions/s/folders/docs", path: "b.png" });
    expect(toolSource(tool("1", "ImageView", "C:\\Users\\you\\proj\\c.png"), "s", "C:\\Users\\you\\proj", [])).toEqual({ base: "/api/sessions/s", path: "c.png" });
    expect(toolSource(tool("1", "ImageView", "/elsewhere/d.png"), "s", "/work", folders)).toBeNull();
  });

  it("lists the conversation's media in reading order, each once", () => {
    const turns = [
      turn("t1", [tool("a", "ImageView", "/work/one.png"), tool("b", "Read", "/work/main.py"), tool("c", "SendFile", "/work/clip.mp4")]),
      turn("t2", [tool("d", "ImageView", "/work/one.png"), tool("e", "ImageView", "/work/two.jpg", { running: true }), tool("f", "Write", "/work/chart.svg"), tool("g", "ImageView", "/work/three.webp")]),
    ];
    const all = conversationMedia(turns, "s", "/work", []);
    expect(all).toEqual([
      { base: "/api/sessions/s", path: "one.png" },
      { base: "/api/sessions/s/sent/c", path: "clip.mp4" },
      { base: "/api/sessions/s", path: "three.webp" },
      { base: "/api/sessions/s", path: "chart.svg" },
    ]);
    expect(galleryFor(all, { base: "/api/sessions/s", path: "three.webp" }).index).toBe(2);
    // A file the list does not hold opens alone rather than at somebody else's picture.
    expect(galleryFor(all, { base: "/api/files/k", path: "kept.png" })).toEqual({ items: [{ base: "/api/files/k", path: "kept.png" }], index: 0 });
  });
});
