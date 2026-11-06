// @vitest-environment jsdom
// A service on a phone: asked for on a press, the Wi-Fi way first, the terms on a private service only.

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang, t } from "./i18n";
import { ADDRESS } from "./publicaddress";
import { PhoneAnswer, PhoneWay, ServicePhone, orderedWays } from "./servicephone";
import { invalidate, prime } from "./store";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function render(node: React.ReactNode): { box: HTMLDivElement; done: () => void } {
  const box = document.createElement("div");
  document.body.appendChild(box);
  const root = createRoot(box);
  act(() => root.render(node));
  return { box, done: () => (act(() => root.unmount()), box.remove()) };
}

const WIFI: PhoneWay = { kind: "wifi", url: "http://lan.example/s/a/", qr: ["11", "11"] };
const TUNNEL: PhoneWay = { kind: "tunnel", url: "https://t.example/s/a/", qr: ["10", "01"] };
const LOCAL: PhoneAnswer = { ways: [TUNNEL, WIFI], mode: "local", code_minutes: 10, pass_hours: 12 };
const PATH = "/api/sessions/s1/services/web%20app/phone";

function host(answer: PhoneAnswer, address: unknown = { native: false }) {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const headers = { "content-type": "application/json" };
    if (String(url).includes(ADDRESS)) return new Response(JSON.stringify(address), { status: 200, headers });
    calls.push(`${init?.method ?? "GET"} ${url}`);
    return new Response(JSON.stringify(init?.method === "DELETE" ? { ok: true } : answer), { status: 200, headers });
  }));
  prime(ADDRESS, address);
  return calls;
}

const press = (box: HTMLElement, selector: string) => {
  const target = box.querySelector(selector) as HTMLElement;
  return act(async () => target.click());
};

beforeEach(() => setLang("en"));
afterEach(() => {
  vi.unstubAllGlobals();
  invalidate(ADDRESS);
});

describe("orderedWays", () => {
  it("puts the same Wi-Fi first", () => {
    expect(orderedWays([TUNNEL, WIFI]).map((way) => way.kind)).toEqual(["wifi", "tunnel"]);
  });
});

describe("ServicePhone", () => {
  it("shows no control for a service without a port", () => {
    const { box, done } = render(<ServicePhone sessionId="s1" name="web" port={null} toast={() => undefined} />);
    expect(box.textContent).toBe("");
    done();
  });

  it("mints nothing until asked, then shows each way with its label and the terms", async () => {
    const calls = host(LOCAL);
    const { box, done } = render(<ServicePhone sessionId="s1" name="web app" port={8123} toast={() => undefined} />);
    await act(async () => undefined);
    expect(calls).toEqual([]);
    await press(box, "[data-phone-open]");
    expect(calls).toEqual([`POST ${PATH}`]);
    expect([...box.querySelectorAll("[data-way]")].map((way) => way.getAttribute("data-way"))).toEqual(["wifi", "tunnel"]);
    expect(box.querySelector("[data-way=wifi]")!.textContent).toContain(t("phone.way.wifi"));
    expect(box.querySelector("[data-way=tunnel]")!.textContent).toContain(t("phone.way.address"));
    expect(box.querySelectorAll("[data-way] svg")).toHaveLength(2);
    expect(box.querySelector("[data-phone-terms]")!.textContent).toBe(t("phone.terms", { minutes: 10, hours: 12 }));
    done();
  });

  it("promises no code on a service that is already shared", async () => {
    host({ ...LOCAL, mode: "key" });
    const { box, done } = render(<ServicePhone sessionId="s1" name="web" port={8123} toast={() => undefined} />);
    await press(box, "[data-phone-open]");
    expect(box.querySelector("[data-phone-terms]")).toBeNull();
    done();
  });

  it("points natively to Settings when there is no way", async () => {
    host({ ...LOCAL, ways: [] }, { native: true, available: false });
    const { box, done } = render(<ServicePhone sessionId="s1" name="web" port={8123} toast={() => undefined} />);
    await press(box, "[data-phone-open]");
    expect(box.querySelector("[data-phone-none]")!.textContent).toContain(t("phone.none.native"));
    expect(box.querySelector("[data-phone-none] a")!.getAttribute("href")).toContain("/settings/address");
    done();
  });

  it("names no Settings section on a server, where it does not exist", async () => {
    host({ ...LOCAL, ways: [] });
    const { box, done } = render(<ServicePhone sessionId="s1" name="web" port={8123} toast={() => undefined} />);
    await press(box, "[data-phone-open]");
    expect(box.querySelector("[data-phone-none]")!.textContent).toBe(t("phone.none"));
    done();
  });

  it("forgets phones and takes the dead codes off the screen", async () => {
    const calls = host(LOCAL);
    const toast = vi.fn();
    const { box, done } = render(<ServicePhone sessionId="s1" name="web app" port={8123} toast={toast} />);
    await press(box, "[data-phone-open]");
    await press(box, "[data-phone-forget]");
    expect(calls).toEqual([`POST ${PATH}`, `DELETE ${PATH}`]);
    expect(box.querySelector("[data-way]")).toBeNull();
    expect(toast).toHaveBeenCalledWith(t("phone.forgotten"));
    done();
  });
});
