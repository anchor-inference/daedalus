// @vitest-environment jsdom
// The secrets manager: a list that never holds a value, one value fetched at a time for the secret that
// is open and dropped again on every way out, a new value sent on its own request and never kept in the
// page's markup, and the draft handed only a name. The values here are invented and plainly so.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang, t } from "./i18n";
import { messageBody } from "./project/goalmodel";

const calls = vi.hoisted(() => ({
  list: { secrets: [] as unknown[], others: [] as unknown[], project_id: null as string | null, project_name: null as string | null } as Record<string, unknown>,
  usage: { conversations: 0, titles: [] as { id: string; title: string }[], audit: [] as unknown[] },
  // The chats list the sidebar holds, read once and kept by the app's cache across the tests.
  sessions: { sessions: [{ id: "sess-1", project_id: "p1" }, { id: "sess-9", project_id: "p2" }], projects: [{ id: "p1", name: "Home network", settings: {} }, { id: "p2", name: "Storage", settings: {} }] },
  posted: [] as [string, Record<string, unknown>][],
  patched: [] as [string, Record<string, unknown>][],
  deleted: [] as string[],
  got: [] as string[],
}));
vi.mock("./api", () => ({
  ApiError: class ApiError extends Error {},
  api: {
    get: async (path: string) => {
      calls.got.push(path);
      if (path === "/api/sessions") return calls.sessions;
      return path.endsWith("/usage") ? calls.usage : calls.list;
    },
    post: async (path: string, body: Record<string, unknown>) => {
      calls.posted.push([path, body]);
      if (path.endsWith("/reveal")) return { value: SHOWN };
      return { ...KNOWN, id: "s1", name: body.name, scope: body.scope, note: body.note, placeholder: `«secret:${String(body.name)}»` };
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

import { AttachedSecret, REVEAL_MS, SecretChip, SecretSheet, SecretsManager, UNDO_MS } from "./secrets";
import { groupByProject, matchesSecret, secretName, sortSecrets, usedBy, validSecretName, type SecretView } from "./secretsmodel";
import { ConfirmHost, ToastHost } from "./ui/dialogs";

/** The one value the host "reveals" here, and the one typed into the form: both invented. */
const SHOWN = "fake-value-for-tests";
const TYPED = "typed-fake-value-1";
const PROJECT = "Home network";
const KNOWN = { id: "s0", name: "wifi", scope: "session", scope_id: "sess-1", scope_title: "Router", note: "home wifi", placeholder: "«secret:wifi»", env: "DAEDALUS_SECRET_WIFI",
  created_at: "2026-11-01T10:00:00Z", updated_at: "2026-11-02T10:00:00Z", last_used_at: "2026-11-20T10:00:00Z", last_used_by: "Exec in sess-1", uses: 7, readable: true };
const PRINTER = { ...KNOWN, id: "s2", name: "printer", note: "office printer", updated_at: "2026-11-10T10:00:00Z", last_used_at: null, last_used_by: "", uses: 0 };
const NAS = { ...KNOWN, id: "s3", name: "nas", scope: "project", scope_id: "p1", scope_title: PROJECT, note: "NAS over SSH", last_used_at: "2026-11-24T10:00:00Z" };
const ELSEWHERE = { ...KNOWN, id: "s9", name: "deploy_key", scope: "project", scope_id: "p2", scope_title: "Storage", note: "" };
const OTHER_CHAT = { ...KNOWN, id: "s8", name: "github_token", scope: "session", scope_id: "sess-9", scope_title: "Release notes", note: "" };

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
  calls.usage = { conversations: 0, titles: [], audit: [] };
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
function press(scope: ParentNode, selector: string) {
  const found = scope.querySelector<HTMLButtonElement>(selector);
  if (!found) throw new Error(`no ${selector}`);
  found.click();
}
function row(name: string): HTMLElement {
  const found = document.querySelector<HTMLElement>(`.secret-row[data-secret="${name}"]`);
  if (!found) throw new Error(`no row ${name}`);
  return found;
}
const names = () => Array.from(document.querySelectorAll<HTMLElement>(".secret-row")).map((r) => r.dataset.secret);
const pane = () => document.querySelector<HTMLElement>(".secret-pane");
/** The question asked over the page: its first button keeps things as they are, its last one acts. */
function answer(yes: boolean) {
  const buttons = document.querySelectorAll<HTMLButtonElement>(".dialog .dialog-actions button");
  if (buttons.length !== 2) throw new Error("no question");
  buttons[yes ? 1 : 0].click();
}
/** Every place a value could hide in the page: the markup, and the attributes a tooltip or a reader reads. */
function inPage(value: string): boolean {
  return document.documentElement.outerHTML.includes(value);
}

async function open(opts: { phone?: boolean; attached?: string[] } = {}) {
  const attached: AttachedSecret[] = [];
  const detached: string[] = [];
  const changed: [string, string | null][] = [];
  let closed = false;
  await act(async () => root.render(
    <><ToastHost /><ConfirmHost />
      <SecretSheet sessionId="sess-1" phone={!!opts.phone} attached={opts.attached ?? []} onClose={() => { closed = true; }}
        onAttach={(s) => attached.push(s)} onDetach={(id) => detached.push(id)} onChanged={(id, name) => changed.push([id, name])} />
    </>,
  ));
  await act(async () => {});
  return { attached, detached, changed, closed: () => closed };
}
function inChat(...secrets: object[]) {
  calls.list = { secrets, others: [], project_id: "p1", project_name: PROJECT };
}

describe("a new secret", () => {
  it("is the first thing a chat with no secret shows, sends its value on its own request and hands the draft only the name", async () => {
    const sheet = await open();
    expect(document.querySelector("#secret-name")).not.toBeNull();
    expect(document.querySelector(".secrets-empty[data-empty='none']")!.textContent).toContain(t("secret.empty.title"));
    expect(button(t("secret.attach")).disabled).toBe(true);
    await act(async () => {
      type("#secret-name", "Router admin");
      type("#secret-value", TYPED);
      type("#secret-note", "ISP router");
    });
    expect(document.body.textContent).toContain("«secret:router_admin»");
    // A typed value lives in the field alone: not in its markup, where a controlled field would put it.
    expect(inPage(TYPED)).toBe(false);
    await act(async () => button(t("secret.attach")).click());
    expect(calls.posted).toEqual([["/api/secrets", { session_id: "sess-1", scope: "session", name: "router_admin", value: TYPED, note: "ISP router" }]]);
    expect(sheet.attached).toEqual([{ id: "s1", name: "router_admin", scope: "session", fresh: true }]);
    expect(JSON.stringify(sheet.attached)).not.toContain(TYPED);
    expect(sheet.closed()).toBe(true);
  });

  it("masks the value until its eye is pressed, in a field no password manager reads as a login", async () => {
    await open();
    const field = document.querySelector<HTMLTextAreaElement>("#secret-value")!;
    expect(field.tagName).toBe("TEXTAREA");
    expect(field.className).not.toContain("shown");
    expect(field.getAttribute("autocomplete")).toBe("off");
    expect(field.getAttribute("data-1p-ignore")).toBe("");
    expect(field.getAttribute("data-lpignore")).toBe("true");
    expect(document.querySelector("#secret-name")!.getAttribute("autocomplete")).toBe("off");
    expect(document.querySelector("input[type=password]")).toBeNull();
    await act(async () => press(document, ".secret-eye"));
    expect(document.querySelector<HTMLTextAreaElement>("#secret-value")!.className).toContain("shown");
  });

  it("offers the project only where the chat has one", async () => {
    await open();
    expect(document.querySelectorAll(".secret-add [role=radio]")).toHaveLength(0);
    await act(async () => root.unmount());
    root = createRoot(host);
    calls.list = { secrets: [], others: [], project_id: "p1", project_name: PROJECT };
    await open();
    await act(async () => Array.from(document.querySelectorAll<HTMLButtonElement>(".secret-add [role=radio]")).find((b) => b.textContent === t("secret.scope.project"))!.click());
    expect(document.body.textContent).toContain(t("secret.scope.project.hint", { project: PROJECT }));
  });

  it("saves without attaching, and opens what it saved", async () => {
    inChat(KNOWN);
    const sheet = await open();
    await act(async () => press(document, ".secrets-add-open"));
    await act(async () => {
      type("#secret-name", "printer");
      type("#secret-value", TYPED);
    });
    calls.list = { secrets: [KNOWN, { ...PRINTER, id: "s1" }], others: [], project_id: "p1", project_name: PROJECT };
    await act(async () => press(document, ".secret-save-only"));
    expect(sheet.attached).toEqual([]);
    expect(sheet.closed()).toBe(false);
    expect(pane()!.dataset.secretPane).toBe("printer");
    expect(inPage(TYPED)).toBe(false);
  });

  it("refuses a name the host would refuse", () => {
    expect(secretName(" Router-Admin ")).toBe("router_admin");
    expect(validSecretName("router admin")).toBe(true);
    expect(validSecretName("1st")).toBe(false);
    expect(validSecretName("пароль")).toBe(false);
  });
});

describe("the list", () => {
  it("opens on the chat's own secrets, with the project's and everything else one press away, the last grouped by project", async () => {
    calls.list = { secrets: [KNOWN, PRINTER, NAS], others: [ELSEWHERE, OTHER_CHAT], project_id: "p1", project_name: PROJECT };
    await open();
    const tabs = Array.from(document.querySelectorAll<HTMLButtonElement>(".secrets-tabs [role=radio]"));
    expect(tabs.map((b) => b.textContent)).toEqual([`${t("secret.tab.chat")}2`, `${t("secret.tab.project")}1`, `${t("secret.tab.all")}5`]);
    expect(names()).toEqual(["wifi", "printer"]);
    await act(async () => tabs[1].click());
    expect(names()).toEqual(["nas"]);
    await act(async () => tabs[2].click());
    const heads = Array.from(document.querySelectorAll(".secrets-group")) as HTMLElement[];
    const groups = heads.map((g) => [g.dataset.group, (Array.from(g.querySelectorAll(".secret-row")) as HTMLElement[]).map((r) => r.dataset.secret)]);
    // The open chat's project first; the other chat's secret goes under the project that chat is in.
    expect(groups).toEqual([["p1", ["nas", "wifi", "printer"]], ["p2", ["deploy_key", "github_token"]]]);
  });

  it("narrows to a search across the views, counts what it found, and says when nothing is", async () => {
    calls.list = { secrets: [KNOWN, PRINTER, NAS], others: [ELSEWHERE], project_id: "p1", project_name: PROJECT };
    await open();
    await act(async () => type(".secrets-search input", "office"));
    expect(names()).toEqual(["printer"]);
    expect(document.querySelector(".secrets-tabs")!.textContent).toContain(`${t("secret.tab.all")}1`);
    await act(async () => type(".secrets-search input", "nothing-like-it"));
    expect(document.querySelector(".secrets-empty[data-empty='search']")!.textContent).toContain(t("secret.search.none", { q: "nothing-like-it" }));
    await act(async () => button(t("secret.search.clear")).click());
    expect(names()).toEqual(["wifi", "printer"]);
  });

  it("says so when the chat has none of its own", async () => {
    inChat(NAS);
    await open();
    // The project's is where the list opens; the chat's own view says it is empty.
    expect(names()).toEqual(["nas"]);
    await act(async () => Array.from(document.querySelectorAll<HTMLButtonElement>(".secrets-tabs [role=radio]"))[0].click());
    expect(document.querySelector(".secrets-empty[data-empty='chat']")!.textContent).toContain(t("secret.empty.chat"));
  });

  it("sorts by last use, by name and by last change", () => {
    const list = [KNOWN, PRINTER, NAS] as SecretView[];
    expect(sortSecrets(list, "used").map((s) => s.name)).toEqual(["nas", "wifi", "printer"]);
    expect(sortSecrets(list, "name").map((s) => s.name)).toEqual(["nas", "printer", "wifi"]);
    expect(sortSecrets(list, "changed").map((s) => s.name)).toEqual(["printer", "nas", "wifi"]);
  });

  it("is drawn in the order the sort asks", async () => {
    inChat(KNOWN, PRINTER);
    await open();
    expect(names()).toEqual(["wifi", "printer"]);
    await act(async () => press(document, ".secrets-sort .dropdown-btn"));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    await act(async () => document.querySelector<HTMLElement>("[role='option'][data-value='changed']")!.click());
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(names()).toEqual(["printer", "wifi"]);
  });

  it("attaches and takes off with one press on a row, leaves the sheet open, and offers nothing it cannot attach", async () => {
    calls.list = { secrets: [KNOWN, PRINTER], others: [ELSEWHERE], project_id: "p1", project_name: PROJECT };
    const sheet = await open({ attached: ["wifi"] });
    const wifi = row("wifi").querySelector<HTMLButtonElement>(".secret-row-attach")!;
    expect(wifi.getAttribute("aria-pressed")).toBe("true");
    await act(async () => wifi.click());
    expect(sheet.detached).toEqual(["s0"]);
    await act(async () => press(row("printer"), ".secret-row-attach"));
    expect(sheet.attached).toEqual([{ id: "s2", name: "printer", scope: "session", fresh: false }]);
    expect(sheet.closed()).toBe(false);
    await act(async () => Array.from(document.querySelectorAll<HTMLButtonElement>(".secrets-tabs [role=radio]")).at(-1)!.click());
    expect(row("deploy_key").querySelector(".secret-row-attach")).toBeNull();
    expect(calls.posted).toEqual([]);
  });

  it("moves with the arrows, searches on /, and starts a new secret on N", async () => {
    inChat(KNOWN, PRINTER);
    await open();
    const rows = () => Array.from(document.querySelectorAll<HTMLButtonElement>(".secret-row-open"));
    // Over a chat the keyboard starts on the open row.
    expect(document.activeElement).toBe(rows()[0]);
    await act(async () => rows()[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true })));
    expect(document.activeElement).toBe(rows()[1]);
    await act(async () => (document.activeElement as HTMLButtonElement).click());
    expect(pane()!.dataset.secretPane).toBe("printer");
    await act(async () => rows()[1].dispatchEvent(new KeyboardEvent("keydown", { key: "/", bubbles: true })));
    expect(document.activeElement).toBe(document.querySelector(".secrets-search input"));
    await act(async () => rows()[1].dispatchEvent(new KeyboardEvent("keydown", { key: "n", bubbles: true })));
    expect(document.querySelector(".secret-add #secret-name")).not.toBeNull();
  });

  it("matches a search on the name, the note or where it belongs", () => {
    const secret = KNOWN as unknown as SecretView;
    expect(matchesSecret(secret, "WIFI")).toBe(true);
    expect(matchesSecret(secret, "home")).toBe(true);
    expect(matchesSecret(secret, "router")).toBe(true);
    expect(matchesSecret(secret, "nas")).toBe(false);
  });

  it("groups a chat's secret under its project only when the project is a real one", () => {
    const sessions = { sessions: [{ id: "sess-1", project_id: "scratch" }, { id: "sess-2", project_id: "p1" }], projects: [{ id: "scratch", name: "Router", settings: { ephemeral: true } }, { id: "p1", name: PROJECT, settings: {} }] };
    const list = [KNOWN, { ...PRINTER, scope_id: "sess-2" }, NAS] as SecretView[];
    expect(groupByProject(list, sessions as never).map((g) => [g.id, g.secrets.map((s) => s.name)])).toEqual([["p1", ["printer", "nas"]], ["", ["wifi"]]]);
  });

  it("reads who used a secret last: a tool in a chat, or on a site", () => {
    expect(usedBy("Exec in 4dc43bdc1ffa")).toEqual({ tool: "Exec", session: "4dc43bdc1ffa" });
    expect(usedBy("BrowserAct (agent) on https://router.example")).toEqual({ tool: "BrowserAct (agent)", site: "https://router.example" });
    expect(usedBy("staff Ann (codex) launch")).toEqual({ tool: "staff Ann (codex) launch" });
  });
});

describe("the open secret", () => {
  it("keeps every value out of the page until its eye is pressed, then hides it again on a second press and on another row", async () => {
    inChat(KNOWN, PRINTER);
    await open();
    expect(pane()!.dataset.secretPane).toBe("wifi");
    expect(pane()!.querySelector(".secret-dots")!.textContent).toMatch(/^•+$/);
    expect(inPage(SHOWN)).toBe(false);
    expect(calls.posted).toEqual([]);
    await act(async () => press(pane()!, ".secret-reveal"));
    expect(calls.posted).toEqual([["/api/secrets/s0/reveal", { purpose: "reveal" }]]);
    expect(pane()!.querySelector(".secret-shown")!.textContent).toBe(SHOWN);
    // Never in a name a tooltip or a screen reader would read.
    expect(pane()!.querySelector(`[aria-label*="${SHOWN}"], [title*="${SHOWN}"]`)).toBeNull();
    await act(async () => press(pane()!, ".secret-reveal"));
    expect(inPage(SHOWN)).toBe(false);
    await act(async () => press(pane()!, ".secret-reveal"));
    expect(inPage(SHOWN)).toBe(true);
    await act(async () => press(row("printer"), ".secret-row-open"));
    expect(inPage(SHOWN)).toBe(false);
    await act(async () => press(row("wifi"), ".secret-row-open"));
    expect(inPage(SHOWN)).toBe(false);
    expect(calls.posted).toHaveLength(2);
  });

  it("hides a revealed value by itself after half a minute, and when the tab is put away", async () => {
    inChat(KNOWN);
    await open();
    vi.useFakeTimers();
    await act(async () => press(pane()!, ".secret-reveal"));
    expect(inPage(SHOWN)).toBe(true);
    await act(async () => { vi.advanceTimersByTime(REVEAL_MS - 1000); });
    expect(inPage(SHOWN)).toBe(true);
    await act(async () => { vi.advanceTimersByTime(1500); });
    expect(inPage(SHOWN)).toBe(false);
    await act(async () => press(pane()!, ".secret-reveal"));
    expect(inPage(SHOWN)).toBe(true);
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    await act(async () => document.dispatchEvent(new Event("visibilitychange")));
    expect(inPage(SHOWN)).toBe(false);
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
  });

  it("takes a revealed value away with the sheet", async () => {
    inChat(KNOWN);
    await open();
    await act(async () => press(pane()!, ".secret-reveal"));
    expect(inPage(SHOWN)).toBe(true);
    await act(async () => root.unmount());
    root = createRoot(host);
    expect(inPage(SHOWN)).toBe(false);
  });

  it("copies through the same audited route without drawing the value, and says so without it", async () => {
    inChat(KNOWN);
    const written: string[] = [];
    Object.defineProperty(navigator, "clipboard", { value: { writeText: async (text: string) => { written.push(text); } }, configurable: true });
    await open();
    await act(async () => press(pane()!, ".secret-copy"));
    expect(calls.posted).toEqual([["/api/secrets/s0/reveal", { purpose: "copy" }]]);
    expect(written).toEqual([SHOWN]);
    expect(inPage(SHOWN)).toBe(false);
    expect(document.body.textContent).toContain(t("secret.copied", { name: "wifi" }));
    expect(pane()!.querySelector(".secret-copy .ic-check")).not.toBeNull();
  });

  it("says where it is used: the tool and chat that used it last, the conversations that name it, and its trail", async () => {
    inChat(KNOWN);
    calls.usage = { conversations: 2, titles: [{ id: "sess-1", title: "Router" }, { id: "sess-7", title: "Firmware" }], audit: [{ at: "2026-11-20T10:00:00Z", name: "wifi", action: "copy", via: "token" }] };
    await open();
    const text = pane()!.textContent!;
    expect(text).toContain("Exec");
    expect(text).toContain("Firmware");
    expect(text).toContain(t("secret.audit.copy"));
    expect(Array.from(pane()!.querySelectorAll<HTMLAnchorElement>(".secret-link")).map((a) => a.getAttribute("href"))).toContain("/app/agents/sess-7");
    expect(calls.got).toContain("/api/secrets/s0/usage");
  });

  it("saves a rename and a note, sending only what changed and never fetching the value", async () => {
    inChat(KNOWN);
    const sheet = await open({ attached: ["wifi"] });
    expect(button(t("secret.save")).disabled).toBe(true);
    await act(async () => {
      type("#secret-edit-s0-name", "Home WiFi");
      type("#secret-edit-s0-note", "the 5 GHz one");
    });
    expect(document.body.textContent).toContain(t("secret.rename.hint", { placeholder: "«secret:home_wifi»" }));
    await act(async () => button(t("secret.save")).click());
    expect(calls.patched).toEqual([["/api/secrets/s0", { name: "home_wifi", note: "the 5 GHz one" }]]);
    expect(calls.posted).toEqual([]);
    expect(sheet.changed).toEqual([["s0", "home_wifi"]]);
    expect(row("home_wifi")).toBeTruthy();
  });

  it("replaces the value under the same name without showing the old one", async () => {
    inChat(KNOWN);
    await open();
    await act(async () => press(pane()!, ".secret-rotate-open"));
    await act(async () => type("#secret-edit-s0-value", TYPED));
    expect(inPage(TYPED)).toBe(false);
    await act(async () => button(t("secret.save")).click());
    expect(calls.patched).toEqual([["/api/secrets/s0", { value: TYPED }]]);
    expect(calls.posted).toEqual([]);
    expect(document.querySelector("#secret-edit-s0-value")).toBeNull();
  });

  it("asks before leaving unsaved edits for another row, a new secret or the sheet's close", async () => {
    inChat(KNOWN, PRINTER);
    const sheet = await open();
    await act(async () => type("#secret-edit-s0-note", "changed but not saved"));
    await act(async () => press(row("printer"), ".secret-row-open"));
    expect(document.querySelector(".dialog")!.textContent).toContain(t("secret.discard.title"));
    await act(async () => answer(false));
    expect(pane()!.dataset.secretPane).toBe("wifi");
    await act(async () => press(document, ".sheet-head [aria-label]:last-child"));
    expect(document.querySelector(".dialog")).not.toBeNull();
    await act(async () => answer(false));
    expect(sheet.closed()).toBe(false);
    await act(async () => press(row("printer"), ".secret-row-open"));
    await act(async () => answer(true));
    expect(pane()!.dataset.secretPane).toBe("printer");
    // Nothing waits now: the close goes straight through.
    await act(async () => press(document, ".sheet-head [aria-label]:last-child"));
    expect(document.querySelector(".dialog")).toBeNull();
    expect(sheet.closed()).toBe(true);
  });

  it("says where it is used and that it leaves the draft before removing it, and holds the request back for the undo", async () => {
    inChat(KNOWN);
    calls.usage = { conversations: 2, titles: [{ id: "a", title: "Router" }, { id: "b", title: "Firmware" }], audit: [] };
    const sheet = await open({ attached: ["wifi"] });
    vi.useFakeTimers();
    await act(async () => press(pane()!, ".secret-remove"));
    const question = document.querySelector(".dialog")!.textContent!;
    expect(question).toContain(t("secret.remove.used", { n: "2", titles: "Router, Firmware" }));
    expect(question).toContain(t("secret.remove.where.session", { title: "Router" }));
    expect(question).toContain(t("secret.remove.draft"));
    await act(async () => answer(true));
    expect(document.querySelector('.secret-row[data-secret="wifi"]')).toBeNull();
    expect(sheet.changed).toEqual([["s0", null]]);
    expect(calls.deleted).toEqual([]);
    await act(async () => button(t("common.undo")).click());
    expect(row("wifi")).toBeTruthy();
    await act(async () => { vi.advanceTimersByTime(UNDO_MS + 100); });
    expect(calls.deleted).toEqual([]);
    await act(async () => press(row("wifi"), ".secret-row-open"));
    await act(async () => press(pane()!, ".secret-remove"));
    await act(async () => answer(true));
    await act(async () => { vi.advanceTimersByTime(UNDO_MS + 100); });
    expect(calls.deleted).toEqual(["/api/secrets/s0"]);
  });

  it("sends a removal still waiting for its undo when the sheet closes", async () => {
    inChat(KNOWN);
    await open();
    await act(async () => press(pane()!, ".secret-remove"));
    await act(async () => answer(true));
    expect(calls.deleted).toEqual([]);
    await act(async () => root.unmount());
    root = createRoot(host);
    expect(calls.deleted).toEqual(["/api/secrets/s0"]);
  });
});

describe("on a phone", () => {
  it("lists first and pushes the open secret over the list, with a way back", async () => {
    inChat(KNOWN, PRINTER);
    await open({ phone: true });
    expect(document.querySelector(".secrets-manager")!.getAttribute("data-layout")).toBe("push");
    expect(pane()).toBeNull();
    await act(async () => press(row("printer"), ".secret-row-open"));
    expect(pane()!.dataset.secretPane).toBe("printer");
    expect(document.querySelector(".secret-row")).toBeNull();
    await act(async () => press(pane()!, ".secret-pane-back"));
    expect(names()).toEqual(["wifi", "printer"]);
  });
});

describe("in Settings", () => {
  it("lists every secret, attaches nothing, opens one over the list in a narrow column, and files a new one under a project", async () => {
    calls.list = { secrets: [KNOWN, ELSEWHERE], projects: [{ id: "p2", name: "Storage" }] };
    let ask = 0;
    const draw = () => root.render(<><ToastHost /><ConfirmHost /><SecretsManager phone={false} requestNew={ask} /></>);
    await act(async () => draw());
    await act(async () => {});
    expect(calls.got).toContain("/api/secrets");
    expect(calls.got.filter((p) => p.startsWith("/api/secrets"))).toEqual(["/api/secrets"]);
    expect(document.querySelectorAll(".secret-row-attach")).toHaveLength(0);
    expect(row("deploy_key")).toBeTruthy();
    // The column has no room beside the list (jsdom measures nothing): the open secret is a sheet.
    expect(document.querySelector(".secrets-manager")!.getAttribute("data-layout")).toBe("sheet");
    expect(inPage(SHOWN)).toBe(false);
    await act(async () => press(row("wifi"), ".secret-row-open"));
    expect(document.querySelector(".secret-pane-sheet .secret-pane")).not.toBeNull();
    await act(async () => press(document, ".secret-pane-sheet .sheet-head [aria-label]:last-child"));
    expect(document.querySelector(".secret-pane-sheet")).toBeNull();
    // The section's header button asks for the form.
    ask = 1;
    await act(async () => draw());
    await act(async () => {
      type("#secret-name", "backup key");
      type("#secret-value", TYPED);
    });
    await act(async () => button(t("secret.save")).click());
    expect(calls.posted).toEqual([["/api/secrets", { project_id: "p2", scope: "project", name: "backup_key", value: TYPED, note: "" }]]);
    expect(inPage(TYPED)).toBe(false);
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
