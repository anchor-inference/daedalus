// @vitest-environment jsdom
// The sources of a turn as the reader meets them: the button under the answer, the panel it opens,
// the popover of a citation chip, and a card picture that fails giving way to its site's mark.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "./i18n";
import { renderMarkdown } from "./md";
import { rememberSources, type TurnSources } from "./rich";
import { SourcesButton, SourcesPanel, hydrateRich } from "./sources";

const SOURCES: TurnSources = {
  cited: [{ url: "https://kaspi.kz/p/1", title: "Pantum on Kaspi", snippet: "Buy it" }],
  more: Array.from({ length: 10 }, (_, i) => ({ url: `https://site${i}.example/page`, title: `Page ${i}` })),
  searches: [{ query: "pantum p2500nw", count: 10 }],
};

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  setLang("en");
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  // The host's mark route: a picture for one site, nothing for the rest.
  vi.stubGlobal("fetch", vi.fn(async (url: string) => (String(url).includes("kaspi.kz")
    ? new Response(new Blob(["x"], { type: "image/png" }), { status: 200, headers: { "content-type": "image/png" } })
    : new Response("", { status: 404 }))));
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:mark" }));
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  document.querySelectorAll(".cite-pop").forEach((el) => el.remove());
  vi.unstubAllGlobals();
});

describe("the sources button and panel", () => {
  it("counts what was cited and read, and opens on a click", () => {
    const open = vi.fn();
    act(() => root.render(<SourcesButton sources={SOURCES} onOpen={open} />));
    const button = host.querySelector("button.sources-button") as HTMLButtonElement;
    expect(button.textContent).toContain("11 sources");
    expect(button.querySelectorAll(".sources-stack .fav")).toHaveLength(3);
    act(() => button.click());
    expect(open).toHaveBeenCalledOnce();
  });

  it("draws nothing for a turn without sources", () => {
    act(() => root.render(<SourcesButton sources={{ cited: [], more: [], searches: [] }} onOpen={() => undefined} />));
    expect(host.innerHTML).toBe("");
  });

  it("lists cited, then more with the rest folded, then the searches", () => {
    act(() => root.render(<SourcesPanel sources={SOURCES} />));
    expect([...host.querySelectorAll(".sources-head")].map((h) => h.textContent)).toEqual(["Cited", "More", "Searches"]);
    expect(host.querySelectorAll(".source-row")).toHaveLength(1 + 8);
    const all = host.querySelector(".sources-all") as HTMLButtonElement;
    expect(all.textContent).toBe("2 more");
    act(() => all.click());
    expect(host.querySelectorAll(".source-row")).toHaveLength(11);
    expect(host.querySelector(".source-search-query")?.textContent).toBe("pantum p2500nw");
    expect(host.querySelector(".source-row")?.getAttribute("rel")).toBe("noreferrer");
  });

  it("says what it is for when no answer is chosen", () => {
    act(() => root.render(<SourcesPanel sources={null} />));
    expect(host.querySelector(".empty")?.textContent).toContain("sources");
  });
});

describe("a rich answer on the page", () => {
  it("paints a site's mark through the host and keeps the letter where there is none", async () => {
    host.innerHTML = renderMarkdown("A.[^](https://kaspi.kz/p/1) B.[^](https://nomark.example/x)");
    hydrateRich(host);
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    const marks = [...host.querySelectorAll<HTMLElement>(".fav")];
    expect(marks[0].classList.contains("has-mark")).toBe(true);
    expect(marks[0].style.backgroundImage).toContain("blob:mark");
    expect(marks[1].classList.contains("has-mark")).toBe(false);
    expect(marks[1].textContent).toBe("N");
  });

  it("opens a chip's sources with the titles the turn knew, and closes on a second tap", () => {
    rememberSources(SOURCES);
    host.innerHTML = renderMarkdown("Cheap.[^](https://kaspi.kz/p/1) [^](https://site3.example/page)");
    const chip = host.querySelector("button.cite") as HTMLButtonElement;
    chip.click();
    const rows = [...document.querySelectorAll(".cite-pop .cite-pop-row")];
    expect(rows.map((r) => r.querySelector(".cite-pop-title")?.textContent)).toEqual(["Pantum on Kaspi", "Page 3"]);
    expect(rows[0].getAttribute("href")).toBe("https://kaspi.kz/p/1");
    chip.click();
    expect(document.querySelector(".cite-pop")).toBeNull();
  });

  it("puts the site's mark where a card's picture failed", () => {
    host.innerHTML = renderMarkdown(":::card\ntitle: X\nurl: https://kaspi.kz/p/1\nimage: https://img.example/broken.jpg\n:::");
    const img = host.querySelector("img.rcard-img") as HTMLImageElement;
    img.dispatchEvent(new Event("error"));
    expect(host.querySelector("img.rcard-img")).toBeNull();
    expect(host.querySelector(".rcard-tile .fav-lg")?.getAttribute("data-host")).toBe("kaspi.kz");
  });
});
