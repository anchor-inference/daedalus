// @vitest-environment jsdom
// A widget's document and its bridge's protocol, without a browser: what the frame is handed, what
// the app accepts back from it, and which calls are drawn at all.

import { describe, expect, it } from "vitest";
import { askText, clampHeight, drawnWidgets, MAX_HEIGHT, MIN_HEIGHT, scriptJson, storagePath, widgetDocument, widgetMessage, WIDGET_CSP } from "./widget";
import type { Activity } from "./turns";

const tool = (id: string, args: Record<string, unknown>, extra: Partial<Extract<Activity, { kind: "tool" }>> = {}): Activity => ({ kind: "tool", id, name: "Widget", args, running: false, ...extra });

const theme = { scheme: "light", theme: "paper", tokens: { "--fg": "#111", "--bg": "#fff" } };

describe("the widgets a turn drew", () => {
  it("keeps finished calls with a document, in order, and nothing else", () => {
    const items: Activity[] = [
      tool("w1", { title: "  Loan   calculator ", html: "<p>a</p>", data: { rate: 4 } }),
      tool("w2", { title: "Running", html: "<p>b</p>" }, { running: true }),
      tool("w3", { title: "Refused", html: "<p>c</p>" }, { error: true }),
      tool("w4", { title: "Empty", html: "   " }),
      { kind: "tool", id: "x", name: "PageShow", args: { slug: "orders" }, running: false },
      tool("w5", { html: "<p>e</p>" }),
    ];
    expect(drawnWidgets(items)).toEqual([
      { callId: "w1", title: "Loan calculator", html: "<p>a</p>", data: { rate: 4 } },
      { callId: "w5", title: "", html: "<p>e</p>", data: null },
    ]);
  });
});

describe("the frame's document", () => {
  it("puts the policy, the look and the bridge before anything of the widget's", () => {
    const doc = widgetDocument("<!doctype html><html><head><script>window.mine = widget.data</script></head><body><p>hi</p></body></html>", { title: "T", data: { n: 1 }, values: { done: [1] }, theme, lang: "ru" });
    const parsed = new DOMParser().parseFromString(doc, "text/html");
    const head = [...parsed.head.children];
    expect(head[0].getAttribute("http-equiv")).toBe("Content-Security-Policy");
    expect(head[0].getAttribute("content")).toBe(WIDGET_CSP);
    expect(WIDGET_CSP).toContain("connect-src 'none'");
    expect(WIDGET_CSP).toContain("default-src 'none'");
    const scripts = [...parsed.querySelectorAll("script")].map((s) => s.textContent ?? "");
    expect(scripts[0]).toContain("window.widget");
    expect(scripts[1]).toBe("window.mine = widget.data");
    expect(parsed.documentElement.lang).toBe("ru");
    expect(parsed.documentElement.dataset.scheme).toBe("light");
    expect(parsed.body.innerHTML).toBe("<p>hi</p>");
  });

  it("takes a fragment, and drops a base or a policy of the widget's own", () => {
    const doc = widgetDocument("<base href='https://example.com/'><meta http-equiv='Content-Security-Policy' content='default-src *'><div id='x'>1</div>", { title: "T", data: null, values: {}, theme, lang: "en" });
    const parsed = new DOMParser().parseFromString(doc, "text/html");
    expect(parsed.querySelector("base")).toBeNull();
    expect(parsed.querySelectorAll("meta[http-equiv]")).toHaveLength(1);
    expect(parsed.getElementById("x")?.textContent).toBe("1");
  });

  it("hands over data that cannot close the bridge's script", () => {
    const doc = widgetDocument("<p>x</p>", { title: "</script><script>alert(1)</script>", data: { s: "<!-- $& $1 -->" }, values: {}, theme, lang: "en" });
    const parsed = new DOMParser().parseFromString(doc, "text/html");
    expect(parsed.querySelectorAll("script")).toHaveLength(1);
    expect(parsed.querySelector("script")?.textContent).toContain('"\\u003c!-- $& $1 --\\u003e"');
    expect(scriptJson("</script>")).toBe('"\\u003c/script\\u003e"');
  });
});

describe("the bridge's messages", () => {
  it("accepts only what the bridge speaks", () => {
    expect(widgetMessage({ widget: 1, kind: "height", height: 240 })).toEqual({ kind: "height", height: 240 });
    expect(widgetMessage({ widget: 1, kind: "set", id: 3, key: "done", value: [1] })).toEqual({ kind: "set", id: 3, key: "done", value: [1] });
    expect(widgetMessage({ widget: 1, kind: "set", id: 3, key: "done" })).toEqual({ kind: "set", id: 3, key: "done", value: null });
    expect(widgetMessage({ widget: 1, kind: "delete", id: 4, key: "done" })).toEqual({ kind: "delete", id: 4, key: "done" });
    expect(widgetMessage({ widget: 1, kind: "ask", id: 5, text: "Why 4%?" })).toEqual({ kind: "ask", id: 5, text: "Why 4%?" });
    for (const bad of [null, "x", { kind: "height", height: 1 }, { widget: 1, kind: "set", key: "k" }, { widget: 1, kind: "set", id: 1, key: "" }, { widget: 1, kind: "navigate", id: 1 }, { daedalus: 1, id: 1, method: "toast" }]) {
      expect(widgetMessage(bad)).toBeNull();
    }
  });

  it("keeps the frame's height between its bounds", () => {
    expect(clampHeight(10)).toBe(MIN_HEIGHT);
    expect(clampHeight(321.2)).toBe(322);
    expect(clampHeight(1e9)).toBe(MAX_HEIGHT);
    expect(clampHeight(Number.NaN)).toBeNull();
    expect(clampHeight("300")).toBeNull();
  });

  it("addresses a key with its slashes and caps what reaches the composer", () => {
    expect(storagePath("s 1", "call/7")).toBe("/api/sessions/s%201/widgets/call%2F7/storage");
    expect(storagePath("s1", "c1", "notes/to day")).toBe("/api/sessions/s1/widgets/c1/storage/notes/to%20day");
    expect(askText("   ")).toBeNull();
    expect(askText("  Why?  ")).toBe("Why?");
    expect(askText("x".repeat(9000))).toHaveLength(4000);
  });
});
