// @vitest-environment jsdom
// The composer's Secret form: the value goes to the host on its own request and never into the draft; what
// the draft keeps is the name, which the message sends and the chat draws as a chip.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang, t } from "./i18n";
import { messageBody } from "./project/goalmodel";

const calls = vi.hoisted(() => ({
  list: { secrets: [] as unknown[], others: [] as unknown[], project_id: null as string | null, project_name: null as string | null },
  usage: { conversations: 0, titles: [] as { id: string; title: string }[] },
  posted: [] as [string, Record<string, unknown>][],
  patched: [] as [string, Record<string, unknown>][],
  deleted: [] as string[],
  got: [] as string[],
}));
vi.mock("./api", () => ({
  api: {
    get: async (path: string) => {
      calls.got.push(path);
      return path.endsWith("/usage") ? calls.usage : calls.list;
    },
    post: async (path: string, body: Record<string, unknown>) => {
      calls.posted.push([path, body]);
      if (path.endsWith("/reveal")) return { value: "fake-value-for-tests" };
      return { id: "s1", name: body.name, scope: body.scope, note: body.note, placeholder: `«secret:${String(body.name)}»` };
    },
    patch: async (path: string, body: Record<string, unknown>) => {
      calls.patched.push([path, body]);
      return { ...KNOWN, ...body };
    },
    delete: async (path: string) => {
      calls.deleted.push(path);
      return { ok: true };
    },
  },
}));

import { AttachedSecret, REVEAL_MS, SecretChip, SecretSheet, SecretsManager, UNDO_MS, matchesSecret, secretName, validSecretName } from "./secrets";
import { ToastHost } from "./ui/dialogs";

const VALUE = "hunter2-and-more";
const PROJECT = "Home network";
const SHOWN = "fake-value-for-tests";
const KNOWN = { id: "s0", name: "wifi", scope: "session", scope_id: "sess-1", scope_title: "Router", note: "home wifi", placeholder: "«secret:wifi»", env: "DAEDALUS_SECRET_WIFI",
  created_at: "2026-11-01T10:00:00Z", updated_at: "2026-11-02T10:00:00Z", last_used_at: null, last_used_by: "", uses: 0, readable: true };
const ELSEWHERE = { ...KNOWN, id: "s9", name: "nas", scope: "project", scope_id: "p2", scope_title: "Storage", note: "" };

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.matchMedia = ((query: string) => ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia;
  setLang("en");
  calls.posted = [];
  calls.patched = [];
  calls.deleted = [];
  calls.got = [];
  calls.usage = { conversations: 0, titles: [] };
  calls.list = { secrets: [], others: [], project_id: null, project_name: null };
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.useRealTimers();
});

function type(selector: string, text: string) {
  const field = document.querySelector<HTMLInputElement | HTMLTextAreaElement>(selector);
  if (!field) throw new Error(`no ${selector}`);
  const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(field), "value")!.set!;
  setter.call(field, text);
  field.dispatchEvent(new Event("input", { bubbles: true }));
}
function button(text: string): HTMLButtonElement {
  const found = Array.from(document.querySelectorAll<HTMLButtonElement>("button")).find((b) => b.textContent?.trim() === text);
  if (!found) throw new Error(`no button ${text}`);
  return found;
}

async function open(phone = false, already: string[] = []) {
  const attached: AttachedSecret[] = [];
  const changed: [string, string | null][] = [];
  let closed = false;
  await act(async () => root.render(<><ToastHost /><SecretSheet sessionId="sess-1" phone={phone} attached={already} onClose={() => { closed = true; }} onAttach={(s) => attached.push(s)} onChanged={(id, name) => changed.push([id, name])} /></>));
  await act(async () => {});
  return { attached, changed, closed: () => closed };
}
function row(name: string): HTMLElement {
  const found = document.querySelector<HTMLElement>(`.secret-row[data-secret="${name}"]`);
  if (!found) throw new Error(`no row ${name}`);
  return found;
}
function press(scope: ParentNode, selector: string) {
  const found = scope.querySelector<HTMLButtonElement>(selector);
  if (!found) throw new Error(`no ${selector}`);
  found.click();
}

describe("the Secret form", () => {
  it("sends the value on its own request and hands the draft only the name", async () => {
    const sheet = await open();
    expect(button(t("secret.attach")).disabled).toBe(true);
    await act(async () => {
      type("#secret-name", "Router admin");
      type("#secret-value", VALUE);
      type("#secret-note", "ISP router");
    });
    expect(document.body.textContent).toContain("«secret:router_admin»");
    await act(async () => button(t("secret.attach")).click());
    expect(calls.posted).toEqual([["/api/secrets", { session_id: "sess-1", scope: "session", name: "router_admin", value: VALUE, note: "ISP router" }]]);
    expect(sheet.attached).toEqual([{ id: "s1", name: "router_admin", scope: "session", fresh: true }]);
    expect(JSON.stringify(sheet.attached)).not.toContain(VALUE);
    expect(sheet.closed()).toBe(true);
  });

  it("masks the value until asked to show it", async () => {
    await open();
    const field = document.querySelector<HTMLTextAreaElement>("#secret-value")!;
    expect(field.className).not.toContain("shown");
    await act(async () => document.querySelector<HTMLButtonElement>(".secret-eye")!.click());
    expect(document.querySelector<HTMLTextAreaElement>("#secret-value")!.className).toContain("shown");
  });

  it("offers the project only where the chat has one, and the chat's secrets to attach again", async () => {
    await open();
    expect(document.body.textContent).not.toContain(t("secret.scope.project"));
    await act(async () => root.unmount());
    root = createRoot(host);
    calls.list = { secrets: [KNOWN], others: [], project_id: "p1", project_name: PROJECT };
    const sheet = await open(true);
    // With something to attach, the sheet opens on the list; the form is one press away.
    expect(document.querySelector("#secret-name")).toBeNull();
    await act(async () => button(t("secret.add")).click());
    expect(document.body.textContent).toContain(t("secret.scope.project"));
    await act(async () => Array.from(document.querySelectorAll<HTMLButtonElement>("[role=radio]")).find((b) => b.textContent === t("secret.scope.project"))!.click());
    expect(document.body.textContent).toContain(t("secret.scope.project.hint", { project: PROJECT }));
    await act(async () => press(row("wifi"), ".secret-row-attach"));
    expect(sheet.attached).toEqual([{ id: "s0", name: "wifi", scope: "session", fresh: false }]);
    expect(sheet.closed()).toBe(true);
    expect(calls.posted).toEqual([]);
  });

  it("refuses a name the host would refuse", () => {
    expect(secretName(" Router-Admin ")).toBe("router_admin");
    expect(validSecretName("router admin")).toBe(true);
    expect(validSecretName("1st")).toBe(false);
    expect(validSecretName("пароль")).toBe(false);
  });
});

describe("the secrets manager", () => {
  it("lists every secret masked, with no value anywhere until the eye is pressed, then hides it again", async () => {
    calls.list = { secrets: [KNOWN], others: [], project_id: null, project_name: null };
    await open();
    expect(row("wifi").textContent).toContain("home wifi");
    expect(row("wifi").querySelector(".secret-row-dots")!.textContent).toMatch(/^•+$/);
    expect(document.body.innerHTML).not.toContain(SHOWN);
    await act(async () => press(row("wifi"), ".secret-reveal"));
    expect(calls.posted).toEqual([["/api/secrets/s0/reveal", { purpose: "reveal" }]]);
    expect(row("wifi").querySelector(".secret-row-dots")!.textContent).toBe(SHOWN);
    await act(async () => press(row("wifi"), ".secret-reveal"));
    expect(document.body.innerHTML).not.toContain(SHOWN);
    expect(calls.posted).toHaveLength(1);
  });

  it("hides a revealed value by itself after half a minute", async () => {
    calls.list = { secrets: [KNOWN], others: [], project_id: null, project_name: null };
    await open();
    vi.useFakeTimers();
    await act(async () => press(row("wifi"), ".secret-reveal"));
    expect(document.body.innerHTML).toContain(SHOWN);
    await act(async () => { vi.advanceTimersByTime(REVEAL_MS - 1000); });
    expect(document.body.innerHTML).toContain(SHOWN);
    await act(async () => { vi.advanceTimersByTime(1500); });
    expect(document.body.innerHTML).not.toContain(SHOWN);
  });

  it("copies through the same audited route without drawing the value", async () => {
    calls.list = { secrets: [KNOWN], others: [], project_id: null, project_name: null };
    const written: string[] = [];
    Object.defineProperty(navigator, "clipboard", { value: { writeText: async (text: string) => { written.push(text); } }, configurable: true });
    await open();
    await act(async () => press(row("wifi"), ".secret-copy"));
    expect(calls.posted).toEqual([["/api/secrets/s0/reveal", { purpose: "copy" }]]);
    expect(written).toEqual([SHOWN]);
    expect(document.body.innerHTML).not.toContain(SHOWN);
    expect(document.body.textContent).toContain(t("secret.row.copied"));
  });

  it("edits in place, sending only what changed and never fetching the value", async () => {
    calls.list = { secrets: [KNOWN], others: [], project_id: null, project_name: null };
    const sheet = await open(false, ["wifi"]);
    await act(async () => press(row("wifi"), ".secret-edit-open"));
    await act(async () => {
      type("#secret-edit-s0-name", "Home WiFi");
      type("#secret-edit-s0-note", "the 5 GHz one");
    });
    expect(document.body.textContent).toContain(t("secret.rename.hint", { placeholder: "«secret:home_wifi»" }));
    await act(async () => button(t("secret.save")).click());
    expect(calls.patched).toEqual([["/api/secrets/s0", { name: "home_wifi", note: "the 5 GHz one" }]]);
    expect(calls.posted).toEqual([]);
    expect(sheet.changed).toEqual([["s0", "home_wifi"]]);
    expect(row("home_wifi").textContent).toContain("the 5 GHz one");
  });

  it("changes a value without showing the old one", async () => {
    calls.list = { secrets: [KNOWN], others: [], project_id: null, project_name: null };
    await open();
    await act(async () => press(row("wifi"), ".secret-edit-open"));
    expect(button(t("secret.save")).disabled).toBe(true);
    await act(async () => type("#secret-edit-s0-value", "a-new-fake-value"));
    await act(async () => button(t("secret.save")).click());
    expect(calls.patched).toEqual([["/api/secrets/s0", { value: "a-new-fake-value" }]]);
    expect(document.body.innerHTML).not.toContain("a-new-fake-value");
  });

  it("warns before removing one that conversations name, and holds the request back for the undo", async () => {
    calls.list = { secrets: [KNOWN], others: [], project_id: null, project_name: null };
    calls.usage = { conversations: 2, titles: [{ id: "a", title: "Router" }, { id: "b", title: "Firmware" }] };
    const sheet = await open(false, ["wifi"]);
    vi.useFakeTimers();
    await act(async () => press(row("wifi"), ".secret-remove"));
    expect(calls.got).toContain("/api/secrets/s0/usage");
    expect(row("wifi").textContent).toContain(t("secret.remove.used", { n: "2", titles: "Router, Firmware" }));
    await act(async () => press(row("wifi"), ".secret-remove-yes"));
    expect(document.querySelector('.secret-row[data-secret="wifi"]')).toBeNull();
    expect(sheet.changed).toEqual([["s0", null]]);
    expect(calls.deleted).toEqual([]);
    await act(async () => button(t("common.undo")).click());
    expect(row("wifi")).toBeTruthy();
    await act(async () => { vi.advanceTimersByTime(UNDO_MS + 100); });
    expect(calls.deleted).toEqual([]);
    // Removed again and left alone, it goes when the undo runs out.
    await act(async () => press(row("wifi"), ".secret-remove"));
    await act(async () => press(row("wifi"), ".secret-remove-yes"));
    await act(async () => { vi.advanceTimersByTime(UNDO_MS + 100); });
    expect(calls.deleted).toEqual(["/api/secrets/s0"]);
  });

  it("sends a removal still waiting for its undo when the sheet closes", async () => {
    calls.list = { secrets: [KNOWN], others: [], project_id: null, project_name: null };
    await open();
    await act(async () => press(row("wifi"), ".secret-remove"));
    await act(async () => press(row("wifi"), ".secret-remove-yes"));
    expect(calls.deleted).toEqual([]);
    await act(async () => root.unmount());
    root = createRoot(host);
    expect(calls.deleted).toEqual(["/api/secrets/s0"]);
  });

  it("keeps other chats' secrets folded below and never offers to attach them", async () => {
    calls.list = { secrets: [KNOWN], others: [ELSEWHERE], project_id: null, project_name: null };
    await open();
    expect(document.querySelector('.secret-row[data-secret="nas"]')).toBeNull();
    await act(async () => press(document, ".secrets-others-toggle"));
    expect(row("nas").querySelector(".secret-row-attach")).toBeNull();
    expect(row("wifi").querySelector(".secret-row-attach")).not.toBeNull();
    // A search opens the fold and narrows both groups.
    await act(async () => press(document, ".secrets-others-toggle"));
    await act(async () => type(".secrets-search input", "storage"));
    expect(row("nas")).toBeTruthy();
    expect(document.querySelector('.secret-row[data-secret="wifi"]')).toBeNull();
  });

  it("marks one already on the draft instead of offering it twice", async () => {
    calls.list = { secrets: [KNOWN], others: [], project_id: null, project_name: null };
    await open(false, ["wifi"]);
    expect(row("wifi").querySelector(".secret-row-attach")).toBeNull();
    expect(row("wifi").textContent).toContain(t("secret.row.attached"));
  });

  it("in Settings lists every secret, attaches nothing, and files a new one under a project", async () => {
    calls.list = { secrets: [KNOWN, ELSEWHERE], projects: [{ id: "p2", name: "Storage" }] } as unknown as typeof calls.list;
    await act(async () => root.render(<SecretsManager phone={false} />));
    await act(async () => {});
    expect(calls.got).toEqual(["/api/secrets"]);
    expect(document.querySelectorAll(".secret-row-attach")).toHaveLength(0);
    expect(row("nas")).toBeTruthy();
    await act(async () => button(t("secret.add")).click());
    await act(async () => {
      type("#secret-name", "backup key");
      type("#secret-value", VALUE);
    });
    await act(async () => button(t("secret.save")).click());
    expect(calls.posted).toEqual([["/api/secrets", { project_id: "p2", scope: "project", name: "backup_key", value: VALUE, note: "" }]]);
  });

  it("matches a search on the name, the note or where it belongs", () => {
    const secret = KNOWN as unknown as Parameters<typeof matchesSecret>[0];
    expect(matchesSecret(secret, "WIFI")).toBe(true);
    expect(matchesSecret(secret, "home")).toBe(true);
    expect(matchesSecret(secret, "router")).toBe(true);
    expect(matchesSecret(secret, "nas")).toBe(false);
  });
});

describe("a message with a secret", () => {
  it("names it in the body and never carries a value", () => {
    expect(messageBody("log in", { clientMessageId: "c1", secrets: ["router_admin"] })).toEqual({ text: "log in", expected_running: false, client_message_id: "c1", secrets: ["router_admin"] });
    expect(messageBody("hi", { clientMessageId: "c2" })).not.toHaveProperty("secrets");
  });

  it("is drawn as a chip with the name", async () => {
    await act(async () => root.render(<SecretChip name="router_admin" scope="project" />));
    const chip = host.querySelector(".secret-chip")!;
    expect(chip.textContent).toBe("router_admin");
    expect(chip.getAttribute("title")).toBe(t("secret.chip.project", { name: "router_admin" }));
  });
});
