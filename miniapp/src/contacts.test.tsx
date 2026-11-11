// @vitest-environment jsdom
// Standing contacts and the operator's own words: what a contact is shown as, that an offer's words stay plain text,
// and that "Write as me" sends what was typed and nothing else.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Contact } from "./api";
import { setLang, t } from "./i18n";
import { invalidate } from "./store";

const world = vi.hoisted(() => ({ contacts: [] as unknown[], posted: [] as [string, unknown][], put: [] as [string, unknown][] }));
vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  api: {
    get: async () => ({ contacts: world.contacts, fingerprint: "AAAA-BBBB-CCCC-DDDD-EEEE", name: "Daedalus", levels: ["read", "all", "trusted"] }),
    post: async (path: string, body?: unknown) => {
      world.posted.push([path, body]);
      return { ok: true };
    },
    put: async (path: string, body?: unknown) => {
      world.put.push([path, body]);
      return { ok: true };
    },
  },
}));

const { ContactsSection, OwnerSay, contactState } = await import("./contacts");
const { guestsWord } = await import("./guestlinks");

const CONTACT: Contact = {
  id: "c1", name: "Mira's Daedalus", state: "active", reason: "", session_id: "s1", fingerprint: "1A2B-3C4D-5E6F-7081-92A3", link_id: "l1", talk_id: "t1", offer_text: "",
  level: "read", usd_limit: 1, turns_per_day: 40, spent_usd: 0.12, created_at: new Date().toISOString(), last_at: new Date().toISOString(), via: "talk",
};

let root: Root;
let host: HTMLDivElement;

beforeEach(() => {
  setLang("en");
  world.contacts = [];
  world.posted = [];
  world.put = [];
  invalidate("/api/contacts");
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

async function settle() {
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}

describe("a contact's state", () => {
  it("asks for the operator on an offer and on a key that did not match", () => {
    expect(contactState({ state: "incoming", reason: "" })).toEqual({ key: "incoming", tone: "attn" });
    expect(contactState({ state: "active", reason: "key" })).toEqual({ key: "key", tone: "attn" });
    expect(contactState({ state: "active", reason: "" })).toEqual({ key: "active", tone: "ok" });
    expect(contactState({ state: "broken", reason: "key" }).key).toBe("broken");
  });

  it("counts the guests of a link only where it takes more than one", () => {
    expect(guestsWord({ max_guests: 1, conversations: [] })).toBe("");
    expect(guestsWord({ max_guests: 5, conversations: [{} as never, {} as never] })).toBe(t("guest.guests.of", { n: 2, of: 5 }));
    expect(guestsWord({ max_guests: 0, conversations: [{} as never] })).toBe(t("guest.guests.free", { n: 1 }));
  });
});

describe("the contacts block", () => {
  it("shows nothing when there are no contacts", async () => {
    act(() => root.render(<ContactsSection sessionId="s1" toast={() => {}} />));
    await settle();
    expect(host.querySelector("[data-contacts]")).toBeNull();
  });

  it("shows both fingerprints, and an offer's words as plain text with accept and decline", async () => {
    world.contacts = [CONTACT, { ...CONTACT, id: "c2", state: "incoming", fingerprint: "FFFF-0000-1111-2222-3333", offer_text: "<b>\u043f\u0440\u0438</b> x", level: "", talk_id: "", link_id: "" }];
    act(() => root.render(<ContactsSection sessionId="s1" toast={() => {}} />));
    await settle();
    expect(host.querySelector("[data-contacts-mine]")?.textContent).toContain("AAAA-BBBB-CCCC-DDDD-EEEE");
    const offer = host.querySelector("[data-contact='c2']")!;
    expect(offer.querySelector("[data-key='verified']")?.textContent).toContain("FFFF-0000-1111-2222-3333");
    expect(offer.querySelector("b")).toBeNull();
    expect(offer.textContent).toContain("<b>\u043f\u0440\u0438</b> x");
    act(() => (offer.querySelector("[data-contact-accept]") as HTMLButtonElement).click());
    await settle();
    expect(world.posted).toContainEqual(["/api/contacts/c2/accept", { session_id: "s1", tools: "read" }]);
  });

  it("changes a contact's level in place", async () => {
    world.contacts = [CONTACT];
    act(() => root.render(<ContactsSection sessionId="s1" toast={() => {}} />));
    await settle();
    const trusted = [...host.querySelectorAll("[data-contact='c1'] [role='radio']")].find((button) => button.textContent === t("contact.level.trusted")) as HTMLButtonElement;
    act(() => trusted.click());
    await settle();
    expect(world.put).toContainEqual(["/api/contacts/c1", { tools: "trusted" }]);
  });
});

describe("writing as the operator", () => {
  it("sends the words as typed, trimmed, and empties the field", async () => {
    const sent = vi.fn();
    act(() => root.render(<OwnerSay path="/api/agent-talks/t1/say" onSent={sent} toast={() => {}} />));
    const field = host.querySelector("textarea")!;
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
    act(() => {
      setter.call(field, "  hello from me  ");
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
    act(() => (host.querySelector("[data-owner-send]") as HTMLButtonElement).click());
    await settle();
    expect(world.posted).toEqual([["/api/agent-talks/t1/say", { text: "hello from me" }]]);
    expect(sent).toHaveBeenCalled();
    expect(field.value).toBe("");
  });
});
