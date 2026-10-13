// @vitest-environment jsdom
// The private sign-in card: a password field that is masked and offers nothing to remember, the values
// sent once on their own request and emptied after, the saved sign-in offered first, and the Inbox entry
// that names the group it answers.

import { act as reactAct } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BrowserGroup } from "../api";
import { setLang } from "../i18n";
import { BrowserLoginCard, loginAsk, loginGroupOf, loginOutcome } from "./login";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function group(saved = false, over: Partial<BrowserGroup> = {}): BrowserGroup {
  return {
    id: "s-abc", owner: { kind: "session", id: "abc", label: "Invoices" }, session_id: "abc", staff_id: null, project_id: null, profile: "p", env: "container",
    status: "running", viewport: { w: 1280, h: 800 }, tabs: [], active_tab: "t1", control: { owner: "paused", holder: null, until: null, reason: "login: sign in" },
    needs_you: { reason: "login", what: "sign in to acme.test: invoices", url: "https://acme.test/login", at: "2026-10-13T10:00:00Z", by: "agent", login: { id: "x1", site: "acme.test", saved, reason: "read the invoices" } },
    acting: false, last_action: null, url: "https://acme.test/login", title: "Sign in", created_at: "", last_activity_at: "", closed_at: null, ...over,
  } as BrowserGroup;
}

let root: Root | null = null;
let host: HTMLDivElement;

async function draw(node: React.ReactNode) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await reactAct(async () => root!.render(node));
}

function typeInto(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  setter.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

function answer(body: unknown, status = 200) {
  return vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

const DONE = { site: "acme.test", url: "https://acme.test/home", signed_in: "yes", filled: ["username", "password"], waiting: "", saved: false };

beforeEach(() => setLang("en"));
afterEach(async () => {
  await reactAct(async () => root?.unmount());
  root = null;
  host?.remove();
  vi.restoreAllMocks();
});

describe("the sign-in form", () => {
  it("masks the password, remembers nothing and sends the values once", async () => {
    const fetch = answer(DONE);
    const told: string[] = [];
    await draw(<BrowserLoginCard group={group()} toast={(text) => told.push(text)} />);
    const form = host.querySelector("form")!;
    expect(form.getAttribute("autocomplete")).toBe("off");
    expect(host.textContent).toContain("Sign in to acme.test");
    expect(host.textContent).toContain("read the invoices");
    const [user, password, code] = Array.from(host.querySelectorAll<HTMLInputElement>("input.field"));
    expect(password.type).toBe("password");
    for (const input of [user, password, code]) expect(input.getAttribute("autocomplete")).toBe("off");
    const submit = host.querySelector<HTMLButtonElement>("button[type=submit]")!;
    expect(submit.disabled).toBe(true);
    await reactAct(async () => {
      typeInto(user, "someone@example.com");
      typeInto(password, "FAKE-PASSWORD-1");
    });
    await reactAct(async () => submit.click());
    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, init] = fetch.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/browsers/s-abc/login");
    expect(JSON.parse(String(init.body))).toEqual({ login_id: "x1", username: "someone@example.com", password: "FAKE-PASSWORD-1", save: false });
    expect(told).toEqual(["Signed in to acme.test; the page is now https://acme.test/home"]);
    // Emptied whatever the answer: nothing typed stays in the page.
    expect(host.querySelector<HTMLInputElement>("input[type=password]")!.value).toBe("");
  });

  it("saves only when asked, and only a password", async () => {
    const fetch = answer({ ...DONE, saved: true });
    await draw(<BrowserLoginCard group={group()} toast={() => undefined} />);
    const [user, password] = Array.from(host.querySelectorAll<HTMLInputElement>("input.field"));
    await reactAct(async () => {
      typeInto(user, "someone@example.com");
      typeInto(password, "FAKE-PASSWORD-2");
      host.querySelector<HTMLInputElement>("input[type=checkbox]")!.click();
    });
    await reactAct(async () => host.querySelector<HTMLButtonElement>("button[type=submit]")!.click());
    expect(JSON.parse(String((fetch.mock.calls[0] as [string, RequestInit])[1].body)).save).toBe(true);
  });

  it("offers a saved sign-in once or for the session before the form", async () => {
    const fetch = answer(DONE);
    await draw(<BrowserLoginCard group={group(true)} toast={() => undefined} />);
    expect(host.textContent).toContain("A sign-in is saved for acme.test");
    expect(host.querySelector("input[type=password]")).toBeNull();
    await reactAct(async () => host.querySelector<HTMLButtonElement>("button[data-use=always]")!.click());
    expect(JSON.parse(String((fetch.mock.calls[0] as [string, RequestInit])[1].body))).toEqual({ login_id: "x1", use_saved: "always" });
    // And the form is a click away.
    await reactAct(async () => Array.from(host.querySelectorAll<HTMLButtonElement>("button")).find((b) => b.textContent === "Enter other credentials")!.click());
    expect(host.querySelector("input[type=password]")).not.toBeNull();
  });

  it("declines, and says a refusal without the values", async () => {
    const fetch = answer({ ok: true });
    const told: string[] = [];
    await draw(<BrowserLoginCard group={group()} toast={(text) => told.push(text)} />);
    await reactAct(async () => host.querySelector<HTMLButtonElement>("button[data-decline]")!.click());
    expect((fetch.mock.calls[0] as [string])[0]).toContain("/api/browsers/s-abc/login/decline");
    expect(told).toEqual(["You declined the sign-in to acme.test"]);
  });

  it("draws nothing for a group that asks nothing", async () => {
    await draw(<BrowserLoginCard group={group(false, { needs_you: null })} toast={() => undefined} />);
    expect(host.innerHTML).toBe("");
    expect(loginAsk(group(false, { status: "closed" }))).toBeNull();
  });

  it("says each outcome, and finds the group of an Inbox entry", () => {
    expect(loginOutcome({ site: "acme.test", url: "https://acme.test/login", signed_in: "no", waiting: "" })).toBe("acme.test did not accept the sign-in; the page is now https://acme.test/login");
    expect(loginOutcome({ site: "acme.test", url: "u", signed_in: "unknown", waiting: "code" })).toBe("acme.test asks for a one-time code; the agent will ask you for it");
    setLang("ru");
    expect(loginOutcome({ site: "acme.test", url: "u", signed_in: "yes", waiting: "" })).toBe("Вход на acme.test выполнен; страница сейчас u");
    expect(loginGroupOf({ kind: "browser_login", request_ref: "browser:s-abc", needs_you: true })).toBe("s-abc");
    expect(loginGroupOf({ kind: "browser_needs_you", request_ref: "browser:s-abc", needs_you: true })).toBeNull();
    expect(loginGroupOf({ kind: "browser_login", request_ref: "browser:s-abc", needs_you: false })).toBeNull();
  });
});
