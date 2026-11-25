// @vitest-environment jsdom
// The page header is one row on a desktop (title, filters, actions in that order), and on a phone the
// filters leave the row for a line of their own under the bar, because a phone has no room beside a
// title for them. The page column names its width as a class the stylesheet reads.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "./i18n";
import { Page, PageHeader } from "./shell";

let host: HTMLDivElement;
let root: Root;

function media(phone: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: phone ? query.includes("max-width") : query.includes("min-width"), media: query, addEventListener() {}, removeEventListener() {} }));
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  setLang("en");
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

const head = (props: Partial<Parameters<typeof PageHeader>[0]> = {}) =>
  act(async () => root.render(<PageHeader title="Inbox" filters={<div className="chips" data-probe="filters" />} actions={<button data-probe="action" />} {...props} />));

describe("PageHeader", () => {
  it("puts the filters and the actions in the title's row on a desktop, the actions last", async () => {
    media(false);
    await head();
    const row = host.querySelector(".pagehead-row")!;
    expect([...row.children].map((c) => c.className)).toEqual(["pagehead-title", "pagehead-filters", "pagehead-actions"]);
    expect(row.querySelector("[data-probe=filters]")).not.toBeNull();
    expect(host.querySelector(".pagehead")!.children).toHaveLength(1);
  });

  it("moves the filters under the bar on a phone", async () => {
    media(true);
    await head();
    const header = host.querySelector(".pagehead")!;
    expect(header.querySelector(".pagehead-row [data-probe=filters]")).toBeNull();
    expect(header.lastElementChild?.getAttribute("data-probe")).toBe("filters");
  });

  it("draws the rule only for a ruled page", async () => {
    media(false);
    await head({ ruled: true });
    expect(host.querySelector(".pagehead")!.classList.contains("ruled")).toBe(true);
    await head({ ruled: false });
    expect(host.querySelector(".pagehead")!.classList.contains("ruled")).toBe(false);
  });

  it("stands a back button before the title", async () => {
    media(false);
    await head({ back: "/app/inbox" });
    const first = host.querySelector(".pagehead-row")!.firstElementChild!;
    expect(first.tagName).toBe("A");
    expect(first.getAttribute("href")).toBe("/app/inbox");
  });
});

describe("Page", () => {
  it("names its column as a class and stays a screen", async () => {
    await act(async () => root.render(<><Page width="reading" className="probe" /><Page /></>));
    const [reading, list] = [...host.querySelectorAll(".screen")];
    expect(reading.className).toBe("screen page-reading probe");
    expect(list.className).toBe("screen page-list");
  });
});
