// @vitest-environment jsdom
// Every picture and clip the app shows can be saved under its own name: the card in the answer, the
// player of a single clip, and the viewer whichever way it was opened. The save address is the
// presentation's /download route with the token a link carries, not the /content address the element
// shows it from: a webview that ignored the download attribute navigated there and lost the app.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MediaPresentation } from "./api";
import { setLang } from "./i18n";
import { FileGallery, InlineMedia, mediaSave } from "./media";

const clip: MediaPresentation = {
  id: "p-clip",
  layout: "single",
  items: [{ id: "v1", kind: "video", mime_type: "video/mp4", filename: "Legion 3D review.mp4", byte_size: 2048, alt: "", caption: "Legion 3D review (1080p, 60 fps, 83 s)" }],
};
const album: MediaPresentation = {
  id: "p-album",
  layout: "album",
  items: [
    { id: "i1", kind: "image", mime_type: "image/png", filename: "front.png", byte_size: 3, width: 4, height: 3, alt: "Front", caption: "" },
    { id: "i2", kind: "image", mime_type: "image/png", filename: "back.png", byte_size: 3, width: 4, height: 3, alt: "Back", caption: "" },
  ],
};

describe("saving attached media", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    setLang("en");
    sessionStorage.setItem("daedalus_token", "tok");
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } })));
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    sessionStorage.clear();
    vi.unstubAllGlobals();
  });

  it("saves from the download route under the original name, and a link from its own origin", () => {
    expect(mediaSave("s", "p-clip", clip.items[0])).toEqual({ href: "/api/sessions/s/media/p-clip/download?path=v1%2FLegion%203D%20review.mp4&token=tok", filename: "Legion 3D review.mp4", outside: false });
    const remote = { ...clip.items[0], url: "https://media.example/clip.mp4" };
    expect(mediaSave("s", "p-clip", remote)).toEqual({ href: "https://media.example/clip.mp4", filename: "Legion 3D review.mp4", outside: true });
  });

  it("puts a download control under a single clip, beside its caption", async () => {
    await act(async () => root.render(<InlineMedia sessionId="s" presentation={clip} />));
    const link = host.querySelector<HTMLAnchorElement>(".inline-media-player-foot a[download]");
    expect(link).not.toBeNull();
    expect(link!.getAttribute("href")).toBe("/api/sessions/s/media/p-clip/download?path=v1%2FLegion%203D%20review.mp4&token=tok");
    expect(link!.getAttribute("download")).toBe("Legion 3D review.mp4");
    expect(link!.getAttribute("target")).toBeNull();
    expect(link!.getAttribute("aria-label")).toBe("Download Legion 3D review.mp4");
    expect(host.querySelector(".inline-media-player-foot figcaption")?.textContent).toBe("Legion 3D review (1080p, 60 fps, 83 s)");
    // The player itself still plays from the content address.
    expect(host.querySelector("video")?.getAttribute("src")).toBe("/api/sessions/s/media/p-clip/v1/content");
  });

  it("offers the download on each picture of an album and in the viewer it opens", async () => {
    await act(async () => root.render(<InlineMedia sessionId="s" presentation={album} />));
    const cards = [...host.querySelectorAll<HTMLAnchorElement>(".inline-media-actions a[download]")];
    expect(cards.map((a) => a.getAttribute("download"))).toEqual(["front.png", "back.png"]);
    await act(async () => host.querySelectorAll<HTMLButtonElement>(".inline-media-surface")[1].click());
    const save = document.querySelector<HTMLAnchorElement>(".media-viewer .lightbox-save");
    expect(save?.getAttribute("href")).toBe("/api/sessions/s/media/p-album/download?path=i2%2Fback.png&token=tok");
    expect(save?.getAttribute("download")).toBe("back.png");
  });

  it("saves a session file from the viewer the files tab opens, without leaving the page", async () => {
    const onPanel = vi.fn();
    await act(async () => root.render(<FileGallery sources={[{ base: "/api/sessions/s", path: "out/demo.mp4" }]} start={0} onClose={() => undefined} onPanel={onPanel} />));
    const save = document.querySelector<HTMLAnchorElement>(".media-viewer .lightbox-save");
    expect(save?.getAttribute("href")).toBe("/api/sessions/s/download?path=out%2Fdemo.mp4&token=tok");
    expect(save?.getAttribute("download")).toBe("demo.mp4");
    // A same-origin download needs no new window; in the desktop app one would be a second, empty window.
    expect(save?.getAttribute("target")).toBeNull();
    // A clip plays in the viewer.
    expect(document.querySelector(".media-viewer-stage video")?.getAttribute("src")).toBe("/api/sessions/s/download?path=out%2Fdemo.mp4&token=tok");
    await act(async () => document.querySelector<HTMLButtonElement>(".media-viewer .lightbox-panel")!.click());
    expect(onPanel).toHaveBeenCalledWith({ base: "/api/sessions/s", path: "out/demo.mp4" });
  });
});
