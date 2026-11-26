// @vitest-environment jsdom
// The files under an answer. A turn that wrote two dozen files drew two dozen full-width cards, each
// with its absolute path (on Windows, `~\AppData\Local\Daedalus\data\workspaces\…\theme.go`), and
// the answer disappeared under them. What the operator reads now: the files sent on purpose as
// cards, three and "+N more"; the files merely written or edited as one "Changed N files" line that
// opens into rows of workspace-relative paths; and no file twice.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "./i18n";
import { prime } from "./store";
import { TurnFiles } from "./artifact";
import { lineChange, placeOf, replacedLines, sumLines, turnFiles } from "./turnfiles";
import { RevealButton } from "./reveal";
import type { Activity } from "./turns";

const tool = (id: string, name: string, path: string, extra: Partial<Extract<Activity, { kind: "tool" }>> = {}): Activity => ({ kind: "tool", id, name, args: { path }, result: "ok", running: false, ...extra });

describe("the lines a turn changed", () => {
  // The changed-files card and the edit steps say "+48 −12". Counted from each call's own text: the
  // lines both sides of a replacement share at the top and the bottom are context, not change.
  it("counts a replacement by the lines that differ, not by both sides whole", () => {
    expect(replacedLines("a\nb\nc", "a\nB\nc")).toEqual({ added: 1, removed: 1 });
    expect(replacedLines("a\nc", "a\nb1\nb2\nc")).toEqual({ added: 2, removed: 0 });
    expect(replacedLines("one line", "one line\n")).toEqual({ added: 0, removed: 0 });
  });

  it("counts a Write as the lines written, a batch edit as its edits, and every match of a replace-all", () => {
    expect(lineChange("Write", { path: "a", content: "x\ny\nz\n" })).toEqual({ added: 3, removed: 0 });
    expect(lineChange("Edit", { path: "a", edits: [{ old_string: "a", new_string: "b" }, { old_string: "c\nd", new_string: "" }] })).toEqual({ added: 1, removed: 3 });
    expect(lineChange("Edit", { path: "a", old_string: "x", new_string: "y", replace_all: true }, "edited a: 4 replacement(s)")).toEqual({ added: 4, removed: 4 });
  });

  it("says nothing it cannot count: a call with no text, and a sum with one of those in it", () => {
    expect(lineChange("Exec", { command: "sed -i s/a/b/ f" })).toBeNull();
    expect(lineChange("Edit", { path: "a" })).toBeNull();
    expect(sumLines([{ added: 1, removed: 0 }, null])).toBeNull();
    const { changed } = turnFiles([tool("e1", "Edit", "a.txt", { args: { path: "a.txt", old_string: "x", new_string: "y\nz" } }), tool("e2", "Edit", "a.txt", { args: { path: "a.txt", old_string: "q", new_string: "" } })]);
    expect(changed[0].lines).toEqual({ added: 2, removed: 2 });
  });
});

describe("where a tool's path is", () => {
  it("is relative to the workspace, whichever way the path is written", () => {
    expect(placeOf("site/menu.html", "/srv/ws/a1")).toEqual({ rel: "site/menu.html", folder: "" });
    expect(placeOf("./site/menu.html", "/srv/ws/a1")).toEqual({ rel: "site/menu.html", folder: "" });
    expect(placeOf("/srv/ws/a1/site/menu.html", "/srv/ws/a1/")).toEqual({ rel: "site/menu.html", folder: "" });
    const windows = "C:\\Users\\someone\\AppData\\Local\\Daedalus\\data\\workspaces\\e3d1ca4e3eba";
    expect(placeOf(`${windows}\\portfwd\\internal\\ui\\theme.go`, windows)).toEqual({ rel: "portfwd/internal/ui/theme.go", folder: "" });
    // A drive letter compares without case, as Windows does.
    expect(placeOf("c:\\users\\someone\\appdata\\local\\daedalus\\data\\workspaces\\e3d1ca4e3eba\\main.go", windows)).toEqual({ rel: "main.go", folder: "" });
    // The home folder as a tilde: the shape in the screenshot that started this.
    expect(placeOf("~\\AppData\\Local\\Daedalus\\data\\workspaces\\e3d1ca4e3eba\\portfwd\\go.mod", windows)).toEqual({ rel: "portfwd/go.mod", folder: "" });
  });

  it("falls back to the project's other folders, and to nothing outside them", () => {
    const folders = [{ id: "f2", path: "/home/someone/work/api" }];
    expect(placeOf("/home/someone/work/api/src/main.py", "/srv/ws/a1", folders)).toEqual({ rel: "src/main.py", folder: "f2" });
    expect(placeOf("/etc/hosts", "/srv/ws/a1", folders)).toBeNull();
    // A sibling whose name only starts like the workspace's is not inside it.
    expect(placeOf("/srv/ws/a10/x.txt", "/srv/ws/a1")).toBeNull();
  });
});

describe("the files of a turn", () => {
  it("splits what was sent from what was changed, and lists nothing twice", () => {
    const items: Activity[] = [
      tool("w1", "Write", "/srv/ws/a1/site/menu.html"),
      tool("e1", "Edit", "site/menu.html"),
      tool("m1", "MultiEdit", "site/robots.txt"),
      tool("w2", "Write", "/srv/ws/a1/reports/check.md"),
      tool("s1", "SendFile", "reports/check.md", { result: "sent check.md (2048 bytes): delivered" }),
      tool("w3", "Write", "broken.txt", { error: true }),
      tool("w4", "Write", "pending.txt", { running: true }),
      tool("r1", "Read", "site/menu.html"),
    ];
    const { sent, changed } = turnFiles(items, "/srv/ws/a1");
    expect(sent.map((f) => [f.name, f.size, f.place?.rel])).toEqual([["check.md", "2 KB", "reports/check.md"]]);
    expect(changed.map((f) => [f.place?.rel, f.how, f.callId])).toEqual([
      ["site/menu.html", "wrote", "w1"],
      ["site/robots.txt", "edited", "m1"],
    ]);
  });

  it("names a folder sent with zip by the archive the host wrote beside it", () => {
    const items: Activity[] = [tool("z1", "SendFile", "reports/", { args: { path: "reports/", zip: true }, result: "shown reports.zip (5120 bytes) — 3 files to the operator" })];
    const { sent } = turnFiles(items, "/srv/ws/a1");
    expect(sent.map((f) => [f.name, f.size, f.place?.rel])).toEqual([["reports.zip", "5 KB", "reports.zip"]]);
  });

  it("counts a screenshot sent in the same step as sent, by the path the host chose, and one kept only as nothing", () => {
    const said = "Tab t1 — https://shop.test/; a PNG of the viewport, 2560×1600 px at 2× its CSS pixels.\nSaved shop.test-20261124-101500.png to /srv/ws/a1/screenshots/shop.test-20261124-101500.png.\n";
    const items: Activity[] = [
      { kind: "tool", id: "b1", name: "BrowserScreenshot", args: { send: true, caption: "the cart" }, result: `${said}shown shop.test-20261124-101500.png (4096 bytes) to the operator as a file card in the app`, running: false },
      { kind: "tool", id: "b2", name: "BrowserScreenshot", args: {}, result: said.replace("101500", "101600").replace("101500", "101600"), running: false },
    ];
    const { sent, changed } = turnFiles(items, "/srv/ws/a1");
    expect(sent.map((f) => [f.name, f.caption, f.size, f.place?.rel])).toEqual([["shop.test-20261124-101500.png", "the cart", "4 KB", "screenshots/shop.test-20261124-101500.png"]]);
    expect(changed).toEqual([]);
  });
});

describe("drawn under the answer", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    setLang("en");
    // A desktop window: the inline list rather than the phone's sheet.
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("min-width"), addEventListener() {}, removeEventListener() {} }));
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 404 })));
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
  });

  const many = (n: number, prefix: string) => Array.from({ length: n }, (_, i) => `${prefix}/file-${String(i).padStart(2, "0")}.go`);

  it("draws three cards and a '+N more', and one line for twenty-three changed files", async () => {
    prime("/api/capabilities", { selfdev: { mode: "off" }, reveal: { available: false, platform: "linux" } });
    const items: Activity[] = [
      ...many(23, "/srv/ws/a1/internal").map((p, i) => tool(`w${i}`, "Write", p)),
      ...["a.md", "b.md", "c.md", "d.md", "e.md"].map((p, i) => tool(`s${i}`, "SendFile", p)),
    ];
    const { sent, changed } = turnFiles(items, "/srv/ws/a1");
    await act(async () => root.render(<TurnFiles sessionId="a1" sent={sent} changed={changed} onOpen={() => undefined} />));
    expect([...host.querySelectorAll(".artifact-name")].map((el) => el.textContent)).toEqual(["a.md", "b.md", "c.md"]);
    expect(host.querySelector(".artifacts-more")?.textContent).toBe("+2 more");
    const head = host.querySelector<HTMLButtonElement>(".changed-files-head")!;
    expect(head.textContent).toContain("Changed 23 files");
    expect(host.querySelectorAll(".changed-file")).toHaveLength(0);

    await act(async () => head.click());
    const paths = [...host.querySelectorAll(".changed-file-path")].map((el) => el.textContent);
    expect(paths).toHaveLength(23);
    expect(paths[0]).toBe("internal/file-00.go");
    expect(paths.some((p) => p?.startsWith("/"))).toBe(false);
    // Not on this machine: nothing offers to open a file manager.
    expect(host.querySelector(".changed-file-reveal")).toBeNull();

    await act(async () => host.querySelector<HTMLButtonElement>(".artifacts-more")!.click());
    expect(host.querySelectorAll(".artifact")).toHaveLength(5);
    expect(host.querySelector(".artifacts-more")).toBeNull();
  });

  it("offers to reveal a row's file when the host is the operator's own machine", async () => {
    prime("/api/capabilities", { selfdev: { mode: "off" }, reveal: { available: true, platform: "windows" } });
    const { sent, changed } = turnFiles([tool("w1", "Write", "/srv/ws/a1/main.go"), tool("w2", "Write", "/elsewhere/x.go")], "/srv/ws/a1");
    await act(async () => root.render(<TurnFiles sessionId="a1" sent={sent} changed={changed} onOpen={() => undefined} />));
    await act(async () => host.querySelector<HTMLButtonElement>(".changed-files-head")!.click());
    const rows = [...host.querySelectorAll(".changed-file")];
    expect(rows[0].querySelector(".changed-file-reveal")?.getAttribute("aria-label")).toBe("Open in Explorer");
    // A file outside every folder of the session cannot be confined by the host, so it is not offered.
    expect(rows[1].querySelector(".changed-file-reveal")).toBeNull();
  });

  it("draws nothing for a turn that touched no file", async () => {
    await act(async () => root.render(<TurnFiles sessionId="a1" sent={[]} changed={[]} onOpen={() => undefined} />));
    expect(host.innerHTML).toBe("");
  });
});

describe("the file manager action", () => {
  let host: HTMLDivElement;
  let root: Root;
  let posted: { url: string; body: unknown }[];
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    setLang("en");
    posted = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      posted.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
      return new Response(JSON.stringify({ path: "/home/someone/work/site", directory: true, opened: true, platform: "macos" }), { status: 200 });
    }));
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    delete window.daedalus;
    vi.unstubAllGlobals();
  });

  it("is not drawn where the file manager is not the operator's", async () => {
    prime("/api/capabilities", { selfdev: { mode: "off" }, reveal: { available: false, platform: "linux" } });
    await act(async () => root.render(<RevealButton target={{ project_id: "p1" }} />));
    expect(host.querySelector("button")).toBeNull();
  });

  it("asks the host to open the folder, or only to check it when the desktop window opens it", async () => {
    prime("/api/capabilities", { selfdev: { mode: "off" }, reveal: { available: true, platform: "macos" } });
    await act(async () => root.render(<RevealButton target={{ project_id: "p1", folder_id: "f2" }} />));
    const button = host.querySelector<HTMLButtonElement>("button")!;
    expect(button.getAttribute("aria-label")).toBe("Show in Finder");
    await act(async () => button.click());
    expect(posted).toEqual([{ url: "/api/reveal", body: { project_id: "p1", folder_id: "f2", run: true } }]);

    const shell = vi.fn(async () => "");
    window.daedalus = { reveal: shell };
    await act(async () => button.click());
    expect(posted[1].body).toEqual({ project_id: "p1", folder_id: "f2", run: false });
    expect(shell).toHaveBeenCalledWith("/home/someone/work/site");
  });
});
