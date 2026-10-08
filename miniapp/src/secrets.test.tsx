// @vitest-environment jsdom
// The composer's Secret form: the value goes to the host on its own request and never into the draft; what
// the draft keeps is the name, which the message sends and the chat draws as a chip.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang, t } from "./i18n";
import { messageBody } from "./project/goalmodel";

const calls = vi.hoisted(() => ({
  list: { secrets: [] as unknown[], project_id: null as string | null, project_name: null as string | null },
  posted: [] as [string, Record<string, unknown>][],
}));
vi.mock("./api", () => ({
  api: {
    get: async () => calls.list,
    post: async (path: string, body: Record<string, unknown>) => {
      calls.posted.push([path, body]);
      return { id: "s1", name: body.name, scope: body.scope, note: body.note, placeholder: `«secret:${String(body.name)}»` };
    },
    delete: async () => ({ ok: true }),
  },
}));

import { AttachedSecret, SecretChip, SecretSheet, secretName, validSecretName } from "./secrets";

const VALUE = "hunter2-and-more";
const PROJECT = "Home network";
const KNOWN = { id: "s0", name: "wifi", scope: "session", note: "", placeholder: "«secret:wifi»", env: "DAEDALUS_SECRET_WIFI" };

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.matchMedia = ((query: string) => ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia;
  setLang("en");
  calls.posted = [];
  calls.list = { secrets: [], project_id: null, project_name: null };
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
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

async function open(phone = false) {
  const attached: AttachedSecret[] = [];
  let closed = false;
  await act(async () => root.render(<SecretSheet sessionId="sess-1" phone={phone} attached={[]} onClose={() => { closed = true; }} onAttach={(s) => attached.push(s)} />));
  await act(async () => {});
  return { attached, closed: () => closed };
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
    calls.list = { secrets: [KNOWN], project_id: "p1", project_name: PROJECT };
    const sheet = await open(true);
    expect(document.body.textContent).toContain(t("secret.scope.project"));
    await act(async () => Array.from(document.querySelectorAll<HTMLButtonElement>("[role=radio]")).find((b) => b.textContent === t("secret.scope.project"))!.click());
    expect(document.body.textContent).toContain(t("secret.scope.project.hint", { project: PROJECT }));
    await act(async () => button("wifi").click());
    expect(sheet.attached).toEqual([{ id: "s0", name: "wifi", scope: "session", fresh: false }]);
    expect(calls.posted).toEqual([]);
  });

  it("refuses a name the host would refuse", () => {
    expect(secretName(" Router-Admin ")).toBe("router_admin");
    expect(validSecretName("router admin")).toBe(true);
    expect(validSecretName("1st")).toBe(false);
    expect(validSecretName("пароль")).toBe(false);
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
