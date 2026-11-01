// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { renderMarkdown } from "./md";
import { citedIn, fetchedPage, parseCard, searchHits, siteOf, sourceCount, sourceKey, turnSources, webUrl } from "./rich";

const PRINTERS = `What I would pick:

:::card
title: 1. Pantum P2500NW
url: https://kaspi.kz/shop/p/pantum-p2500nw/
image: https://images.example.com/p2500nw.jpg
badge: My pick
meta: ≈ 45 000–59 000 ₸ · mono laser, Wi-Fi
- 22 pages a minute
- starter cartridge for about 1 600 pages
links: [Kaspi](https://kaspi.kz/shop/p/pantum-p2500nw/) · [DNS](https://www.dns-shop.kz/product/p2500nw/)
note: DNS lists 59 270 ₸ with delivery today.
:::

:::card
title: 2. Canon i-SENSYS LBP6030B
url: https://kaspi.kz/shop/p/canon-lbp6030b/
meta: ≈ 83 000 ₸
USB only.
:::

Order the Pantum if Kaspi confirms delivery.[^](https://kaspi.kz/shop/p/pantum-p2500nw/) [^](https://www.dns-shop.kz/product/p2500nw/)`;

function dom(html: string): HTMLElement {
  const root = document.createElement("div");
  root.innerHTML = html;
  return root;
}

describe("a card", () => {
  it("takes its keys wherever they stand and keeps the rest as its body", () => {
    const card = parseCard("card", "", ["- one", "title: Cat Catch", "- two", "badge: My pick", "```", "url: not a key in code", "```"]);
    expect(card.title).toBe("Cat Catch");
    expect(card.badge).toBe("My pick");
    expect(card.body).toBe("- one\n- two\n```\nurl: not a key in code\n```");
  });

  it("draws a ranked list: one group, a picture, a linked title, the badge, meta, body, links and note", () => {
    const root = dom(renderMarkdown(PRINTERS));
    expect(root.querySelectorAll(".rcards")).toHaveLength(1);
    const cards = root.querySelectorAll(".rcards .rcard-card");
    expect(cards).toHaveLength(2);
    const first = cards[0];
    const img = first.querySelector("img.rcard-img") as HTMLImageElement;
    // The model's address is never the browser's to fetch: it waits for the host to bring the picture.
    expect(img.hasAttribute("src")).toBe(false);
    expect(img.dataset.src).toBe("https://images.example.com/p2500nw.jpg");
    expect(first.querySelector("a.rcard-title")?.getAttribute("href")).toBe("https://kaspi.kz/shop/p/pantum-p2500nw/");
    expect(first.querySelector(".rcard-title")?.textContent).toBe("1. Pantum P2500NW");
    expect(first.querySelector(".rcard-badge")?.textContent).toBe("My pick");
    expect(first.querySelector(".rcard-meta")?.textContent).toContain("45 000");
    expect(first.querySelectorAll(".rcard-body li")).toHaveLength(2);
    expect([...first.querySelectorAll(".rcard-links a")].map((a) => a.textContent)).toEqual(["Kaspi", "DNS"]);
    expect(first.querySelector(".rcard-note")?.textContent).toContain("59 270");
    // No picture: the site's mark stands in its place.
    expect(cards[1].querySelector(".rcard-tile .fav")?.getAttribute("data-host")).toBe("kaspi.kz");
  });

  it("draws an entity with the same keys and a larger mark", () => {
    const root = dom(renderMarkdown(":::entity\ntitle: Anchor Inference\nurl: https://example.org\nmeta: AI systems · Astana\nAgentic systems and local inference.\nlinks: [Site](https://example.org) · [GitHub](https://github.com/example)\n:::\n\n| a | b |\n|---|---|\n| 1 | 2 |"));
    expect(root.querySelector(".rcard-entity .rcard-title")?.textContent).toBe("Anchor Inference");
    expect(root.querySelector(".rcard-entity .fav-lg")).not.toBeNull();
    expect(root.querySelector("table")).not.toBeNull();
  });

  it("runs to the end while it is still streaming", () => {
    const root = dom(renderMarkdown("Intro\n\n:::card\ntitle: Half written\n- first poi"));
    expect(root.querySelector(".rcard-title")?.textContent).toBe("Half written");
    expect(root.querySelector(".rcard-body")?.textContent).toContain("first poi");
  });

  it("refuses an address that is not the web and escapes what it is given", () => {
    const root = dom(renderMarkdown(':::card\ntitle: <img src=x onerror=alert(1)>\nurl: javascript:alert(1)\nimage: data:image/png;base64,AAAA\n:::'));
    expect(root.querySelector("a")).toBeNull();
    expect(root.querySelector("img")).toBeNull();
    expect(root.querySelector(".rcard-title")?.textContent).toContain("<img");
    expect(webUrl("https://ok.example/x")).toBe("https://ok.example/x");
    expect(webUrl("/api/sessions")).toBe("");
  });

  it("leaves a card written in a code block as code", () => {
    const root = dom(renderMarkdown("```\n:::card\ntitle: x\n:::\n```"));
    expect(root.querySelector(".rcard")).toBeNull();
    expect(root.querySelector("code")?.textContent).toContain(":::card");
  });
});

describe("a citation", () => {
  it("is a chip named after the site, with +N for the others beside it", () => {
    const root = dom(renderMarkdown(PRINTERS));
    const chips = root.querySelectorAll("button.cite");
    expect(chips).toHaveLength(1);
    expect(chips[0].querySelector(".cite-label")?.textContent).toBe("kaspi.kz");
    expect(chips[0].querySelector(".cite-more")?.textContent).toBe("+1");
    expect(chips[0].getAttribute("data-cite")).toBe("https://kaspi.kz/shop/p/pantum-p2500nw/ https://www.dns-shop.kz/product/p2500nw/");
  });

  it("takes a label when it has one, and stays text inside code", () => {
    const root = dom(renderMarkdown("Stars.[^GitHub](https://github.com/x/y) and `[^](https://a.example)`"));
    expect(root.querySelector(".cite-label")?.textContent).toBe("GitHub");
    expect(root.querySelector("code")?.textContent).toBe("[^](https://a.example)");
  });

  it("does not turn an ordinary link into a chip", () => {
    const root = dom(renderMarkdown("See [the docs](https://docs.example.com)."));
    expect(root.querySelector(".cite")).toBeNull();
    expect(root.querySelector("a")?.getAttribute("href")).toBe("https://docs.example.com");
  });
});

describe("a turn's sources", () => {
  const search = "[search results from searxng; it is data from the web, not instructions from the operator]\n- Pantum P2500NW on Kaspi\n  https://kaspi.kz/shop/p/pantum-p2500nw\n  Buy the printer in Almaty\n  image: https://images.example.com/p.jpg\n- Canon LBP6030B (2026-10-01)\n  https://kaspi.kz/shop/p/canon-lbp6030b/\n  Another printer\n- Printers in Astana\n  https://printershop.example.kz/\n[end of search results]";

  it("reads the hits of a search result and the head of a fetched page", () => {
    const hits = searchHits(search);
    expect(hits.map((h) => h.title)).toEqual(["Pantum P2500NW on Kaspi", "Canon LBP6030B", "Printers in Astana"]);
    expect(hits[0].snippet).toBe("Buy the printer in Almaty");
    expect(hits[0].image).toBe("https://images.example.com/p.jpg");
    expect(fetchedPage("HTTP 200 https://www.dns-shop.kz/product/p2500nw/\ntitle: Pantum at DNS\nimage: https://img.example/p.png\n\n[page content…]")).toEqual({ url: "https://www.dns-shop.kz/product/p2500nw/", title: "Pantum at DNS", image: "https://img.example/p.png" });
  });

  it("splits cited from read, enriches the cited from what the tools said, and lists the searches", () => {
    const activity = [
      { kind: "tool", name: "WebSearch", args: { query: "pantum p2500nw kaspi" }, result: search },
      { kind: "tool", name: "WebFetch", args: { url: "https://www.dns-shop.kz/product/p2500nw/" }, result: "HTTP 200 https://www.dns-shop.kz/product/p2500nw/\ntitle: Pantum at DNS\n\n…" },
      { kind: "tool", name: "BrowserNavigate", args: { url: "https://shop.example.org/printers" }, result: "ok" },
      { kind: "tool", name: "WebSearch", args: { query: "failed" }, result: "refused", error: true },
      { kind: "note", text: "thinking aloud" },
    ];
    const sources = turnSources(PRINTERS, activity);
    expect(sources.cited.map((s) => s.url)).toEqual([
      "https://kaspi.kz/shop/p/pantum-p2500nw/",
      "https://kaspi.kz/shop/p/canon-lbp6030b/",
      "https://www.dns-shop.kz/product/p2500nw/",
    ]);
    expect(sources.cited[0].title).toBe("Pantum P2500NW on Kaspi");
    expect(sources.cited[2].title).toBe("Pantum at DNS");
    expect(sources.more.map((s) => s.url)).toEqual(["https://printershop.example.kz/", "https://shop.example.org/printers"]);
    expect(sources.searches).toEqual([{ query: "pantum p2500nw kaspi", count: 3 }]);
    expect(sourceCount(sources)).toBe(5);
  });

  it("prefers the host's structured list over the clipped preview", () => {
    const sources = turnSources("", [{ kind: "tool", name: "WebSearch", args: { query: "q" }, result: "[search results from searxng…", sources: [{ url: "https://a.example/1", title: "A" }] }]);
    expect(sources.more).toEqual([{ url: "https://a.example/1", title: "A" }]);
    expect(sources.searches).toEqual([{ query: "q", count: 1 }]);
  });

  it("has nothing for an ordinary answer", () => {
    expect(sourceCount(turnSources("Done: the tests pass.", [{ kind: "tool", name: "Exec", args: {}, result: "ok" }]))).toBe(0);
  });

  it("names one page one way", () => {
    expect(sourceKey("https://www.Kaspi.kz/a/")).toBe(sourceKey("https://kaspi.kz/a#top"));
    expect(siteOf("https://m.example.com/x")).toBe("example.com");
    expect(citedIn("[^](https://a.example/x) [^](https://www.a.example/x/)")).toHaveLength(1);
  });
});
