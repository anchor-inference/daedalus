// @vitest-environment jsdom
// A page an agent showed the operator (PageShow) is a card in the conversation that opens the page's
// screen. The card draws the page as the host has it now — title in the reader's language, the
// version that opens, the page's own icon — and says so plainly when the page has been removed.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "./i18n";
import { prime } from "./store";
import { PageCards, pageTitle, shownPath } from "./pagecard";
import { turnPages } from "./turnfiles";
import type { Activity } from "./turns";

const show = (id: string, slug: string, extra: Partial<Extract<Activity, { kind: "tool" }>> = {}, draft = false): Activity => ({ kind: "tool", id, name: "PageShow", args: { slug, ...(draft ? { draft: true } : {}) }, result: "ok", running: false, ...extra });

describe("the pages a turn showed", () => {
  it("keeps each page once, the last showing winning, and leaves out failed and running calls", () => {
    const items: Activity[] = [
      show("a", "orders"),
      { kind: "tool", id: "w", name: "PageWrite", args: { slug: "orders", path: "index.html" }, result: "ok", running: false },
      show("b", "cad", {}, true),
      show("c", "orders", {}, true),
      show("d", "gone", { error: true }),
      show("e", "later", { running: true }),
      show("f", "../evil"),
      show("g", "калькулятор"),
      show("h", "Калькулятор"),
    ];
    expect(turnPages(items)).toEqual([
      { callId: "b", slug: "cad", draft: true },
      { callId: "c", slug: "orders", draft: true },
      { callId: "g", slug: "калькулятор", draft: false },
    ]);
  });

  it("opens the page's screen, on the draft when the draft was shown", () => {
    expect(shownPath({ callId: "x", slug: "orders", draft: false })).toBe("/app/p/orders");
    expect(shownPath({ callId: "x", slug: "orders", draft: true })).toBe("/app/p/orders?draft=1");
    expect(pageTitle({ en: "Orders", ru: "Заказы" }, "ru", "orders")).toBe("Заказы");
    expect(pageTitle({ de: "Bestellungen" }, "ru", "orders")).toBe("Bestellungen");
    expect(pageTitle(undefined, "en", "orders")).toBe("orders");
  });
});

describe("the card", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    setLang("en");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "no page 'gone'" }), { status: 404, headers: { "Content-Type": "application/json" } })));
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
    setLang("en");
  });

  const detail = (slug: string, extra: Record<string, unknown> = {}) => ({
    slug, title: { en: "Orders", ru: "Заказы" }, titles: { en: "Orders", ru: "Заказы" }, icon: "board", icon_path: null, path: `/app/p/${slug}`,
    version: 3, updated_at: null, kind: "static", entry: "index.html", target: null, session_id: null, url: `/pages/${slug}/`, draft_url: `/pages/${slug}/?draft=1`, versions: [], ...extra,
  });

  it("draws the page's title, version and icon, and Open goes to the page", async () => {
    prime("/api/pages/orders", detail("orders"));
    await act(async () => root.render(<PageCards pages={[{ callId: "c1", slug: "orders", draft: false }]} />));
    const card = host.querySelector<HTMLElement>(".page-card")!;
    expect(card.dataset.page).toBe("orders");
    expect(card.querySelector(".artifact-name")?.textContent).toBe("Orders");
    expect(card.querySelector(".artifact-meta")?.textContent).toBe("Page · version 3");
    expect(card.querySelector(".ic-board")).not.toBeNull();
    await act(async () => card.querySelector<HTMLButtonElement>(".page-card-open")!.click());
    expect(window.location.pathname + window.location.search).toBe("/app/p/orders");
  });

  it("speaks the reader's language, names the draft, and draws a page's own glyph", async () => {
    setLang("ru");
    prime("/api/pages/cad", detail("cad", { title: { en: "Viewer", ru: "Просмотр" }, icon: "no-such-icon", icon_path: "M4 4h16v16H4z", version: null }));
    await act(async () => root.render(<PageCards pages={[{ callId: "c2", slug: "cad", draft: true }]} />));
    expect(host.querySelector(".artifact-name")?.textContent).toBe("Просмотр");
    expect(host.querySelector(".artifact-meta")?.textContent).toBe("Страница · черновик");
    expect(host.querySelector(".artifact-glyph path")?.getAttribute("d")).toBe("M4 4h16v16H4z");
    expect(host.querySelector(".page-card-open")?.textContent).toBe("Открыть");
    await act(async () => host.querySelector<HTMLButtonElement>(".artifact-main")!.click());
    expect(window.location.pathname + window.location.search).toBe("/app/p/cad?draft=1");
  });

  it("falls back to the grid icon for an icon the app does not have", async () => {
    prime("/api/pages/x", detail("x", { icon: "no-such-icon", version: null }));
    await act(async () => root.render(<PageCards pages={[{ callId: "c3", slug: "x", draft: false }]} />));
    expect(host.querySelector(".ic-grid")).not.toBeNull();
    expect(host.querySelector(".artifact-meta")?.textContent).toBe("Page · not published yet");
  });

  it("says a removed page is gone and offers nothing to open", async () => {
    await act(async () => root.render(<PageCards pages={[{ callId: "c4", slug: "gone", draft: false }]} />));
    await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
    expect(host.querySelector(".artifact-meta")?.textContent).toBe("This page has been removed");
    expect(host.querySelector<HTMLButtonElement>(".page-card-open")?.disabled).toBe(true);
  });

  it("draws nothing for a turn that showed no page", async () => {
    await act(async () => root.render(<PageCards pages={[]} />));
    expect(host.innerHTML).toBe("");
  });
});
