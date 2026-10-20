// @vitest-environment jsdom
// A file the agent attached into its answer (AttachMedia with kind "file") stands where the answer
// placed it, as a file card that opens in Preview. It needs no media credential, so it must not wait
// behind the cookie upgrade a picture needs.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MediaPresentation } from "./api";
import { setLang } from "./i18n";
import { InlineMedia, attachedSource, splitMediaAnswer } from "./media";
import type { PreviewSource } from "./preview";

// The answer names a presentation by its uuid; a reference that is not one stays text.
const pid = "33333333-3333-3333-3333-333333333333";
const presentation: MediaPresentation = {
  id: pid,
  layout: "single",
  items: [{ id: "i", kind: "file", mime_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename: "x.docx", byte_size: 2048, alt: "The act", caption: "Signed copy" }],
};

describe("a file attached into an answer", () => {
  let host: HTMLDivElement;
  let root: Root;
  let fetches: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    setLang("en");
    fetches = vi.fn(async () => new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetches);
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
  });

  it("is read from the item's path under the presentation, named by the file", () => {
    expect(attachedSource("s", "p", presentation.items[0])).toEqual({ base: "/api/sessions/s/media/p", path: "i/x.docx" });
  });

  it("draws a card in place that opens the file in Preview", async () => {
    const parts = splitMediaAnswer(`Here is the act.\n\n![The act](daedalus-media:${pid})\n\nSign it.`, [presentation]);
    expect(parts.map((part) => part.kind)).toEqual(["text", "media", "text"]);
    const media = parts[1];
    if (media.kind !== "media") throw new Error("no media part");
    const opened: PreviewSource[] = [];
    await act(async () => root.render(<InlineMedia sessionId="s" presentation={media.presentation} onPreview={(src) => opened.push(src)} />));
    expect(host.querySelector(".inline-media-loading")).toBeNull();
    expect(fetches).not.toHaveBeenCalled();
    expect(host.querySelector(".artifact-name")?.textContent).toBe("x.docx");
    const meta = host.querySelector(".artifact-meta")?.textContent ?? "";
    expect(meta).toContain("Signed copy");
    expect(meta).toContain("docx");
    expect(meta).toContain("attached");
    expect(host.querySelector<HTMLAnchorElement>(".artifact-actions a")?.getAttribute("href")).toBe(`/api/sessions/s/media/${pid}/download?path=i%2Fx.docx`);
    await act(async () => host.querySelector<HTMLButtonElement>(".artifact-main")!.click());
    expect(opened).toEqual([{ base: `/api/sessions/s/media/${pid}`, path: "i/x.docx" }]);
  });
});
