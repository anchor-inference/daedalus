// @vitest-environment jsdom
// Remote chats and pairing: the folder shortened for a header, the state a remote chat's header says
// (a paused turn outranking the link), the code compared however it was typed, the time a request has
// left, the paused banner's Cancel turn, the approval sheet that posts the code it shows and says so
// when nobody waits with a code, and the Devices page's revoke that asks in the row before it acts.

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, type CliDevice, type PairRequest, type RemoteBinding } from "./api";
import { setLang } from "./i18n";
import { PairSheet, RECONNECT_GRACE_MS, RemoteHead, RemotePaused, findRequest, normCode, noteConnection, osLabel, remoteOf, remoteRowWords, remoteState, shortFolder, showCode, timeLeft } from "./remote";
import { DevicesPanel, loginCommand, tokenAge } from "./screens/DevicesSettings";
import { prime } from "./store";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const BOUND: RemoteBinding = { device_id: "d1", device_name: "laptop-graceful-unicorn", cwd: "/home/you/work/bakery/site", connected: true, guard: "auto", waiting: null };
const WAITING = { since: "2026-11-02T11:58:00Z", calls: [{ tool_call_id: "c1", tool: "Exec" }] };
const NOW = Date.parse("2026-11-02T12:00:00Z");
const REQUEST: PairRequest = { id: "p1", user_code: "WDJB-MJHT", name: "laptop-graceful-unicorn", hostname: "laptop", os: "darwin", arch: "arm64", client_version: "0.1.0",
  address: "127.0.0.1", created_at: "2026-11-02T11:59:30Z", expires_at: "2026-11-02T12:10:00Z", state: "pending" };

describe("what a remote chat's header says", () => {
  it("shortens a folder to the home and its last two parts", () => {
    expect(shortFolder("/home/you/site")).toBe("~/site");
    expect(shortFolder("/Users/you/work/bakery/site")).toBe("…/bakery/site");
    expect(shortFolder("C:\\Users\\you\\code\\app")).toBe("~\\code\\app");
    expect(shortFolder("/srv/data")).toBe("/srv/data");
    expect(shortFolder("/home/you")).toBe("~");
  });

  it("puts a paused turn before the link, as a row's yes or the view's parked calls", () => {
    expect(remoteState(BOUND)).toBe("connected");
    expect(remoteState({ ...BOUND, connected: false, waiting: WAITING })).toBe("paused");
    expect(remoteState({ ...BOUND, connected: false, waiting: true })).toBe("paused");
    expect(remoteState({ ...BOUND, device_id: "never-seen", connected: false })).toBe("offline");
  });

  it("says a device reconnects for a while after this window saw it drop, then offline", () => {
    noteConnection("d2", false, NOW);
    const down = { ...BOUND, device_id: "d2", connected: false };
    expect(remoteState(down, NOW + 1000)).toBe("reconnecting");
    expect(remoteState(down, NOW + RECONNECT_GRACE_MS + 1)).toBe("offline");
    noteConnection("d2", true, NOW + 2000);
    expect(remoteState(down, NOW + 3000)).toBe("offline");
  });

  it("tells reconnecting from offline by the server's moment in a window that did not see the drop", () => {
    const down = { ...BOUND, device_id: "d3", connected: false, disconnected_at: new Date(NOW - 5000).toISOString() };
    expect(remoteState(down, NOW)).toBe("reconnecting");
    expect(remoteState(down, NOW + RECONNECT_GRACE_MS)).toBe("offline");
    expect(remoteState({ ...down, disconnected_at: null }, NOW)).toBe("offline");
  });

  it("names the system as people do", () => {
    expect(osLabel("darwin", "arm64")).toBe("macOS · ARM");
    expect(osLabel("linux", "amd64")).toBe("Linux · x86-64");
    expect(osLabel("plan9")).toBe("plan9");
  });

  it("takes the binding only from a remote chat", () => {
    expect(remoteOf({ kind: "remote", remote: BOUND })).toBe(BOUND);
    expect(remoteOf({ kind: "chat", remote: BOUND })).toBeNull();
    expect(remoteOf({ kind: "remote" })).toBeNull();
    expect(remoteRowWords({ kind: "remote", remote: { ...BOUND, waiting: true } })).toBe("laptop-graceful-unicorn · Paused");
    expect(remoteRowWords({ kind: "remote", remote: BOUND })).toBe("laptop-graceful-unicorn");
  });
});

describe("a pairing code", () => {
  it("is compared whatever its case, spaces or dash", () => {
    expect(normCode(" wdjb-mjht ")).toBe("WDJBMJHT");
    expect(showCode("wdjbmjht")).toBe("WDJB-MJHT");
    expect(findRequest([REQUEST], "wdjb mjht")).toBe(REQUEST);
    expect(findRequest([REQUEST], "WDJBMJHT")).toBe(REQUEST);
    expect(findRequest([REQUEST], "WDJB-MJHX")).toBeNull();
  });

  it("says how long it has left, and nothing once it is out of time", () => {
    expect(timeLeft(REQUEST.expires_at, NOW)).toBe("10:00");
    expect(timeLeft(REQUEST.expires_at, NOW + 9 * 60_000 + 55_000)).toBe("0:05");
    expect(timeLeft(REQUEST.expires_at, NOW + 11 * 60_000)).toBe("");
  });

  it("gives the token's age in days, and the login command for this address", () => {
    expect(tokenAge("2026-11-02T08:00:00Z", NOW)).toBe("issued today");
    expect(tokenAge("2026-10-30T08:00:00Z", NOW)).toBe("3 days old");
    expect(loginCommand("http://127.0.0.1:8765")).toBe("daedalus-cli login http://127.0.0.1:8765");
  });
});

describe("the remote parts on screen", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    setLang("en");
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("names the computer, the folder, the link and a guard that is not the default in the header", () => {
    act(() => root.render(<RemoteHead b={{ ...BOUND, connected: false }} state="reconnecting" />));
    const head = host.querySelector(".remote-head")!;
    expect(head.getAttribute("data-remote-state")).toBe("reconnecting");
    expect(head.textContent).toContain("laptop-graceful-unicorn");
    expect(head.textContent).toContain("…/bakery/site");
    expect(head.textContent).toContain("Reconnecting…");
    expect(head.querySelector(".remote-guard")).toBeNull();
    expect(head.getAttribute("title")).toContain("/home/you/work/bakery/site");
    act(() => root.render(<RemoteHead b={{ ...BOUND, guard: "plan" }} state="connected" />));
    expect(host.querySelector(".remote-guard")!.textContent).toBe("plan, read-only");
  });

  it("shows the paused banner only for a paused turn, and cancels the turn from it", async () => {
    const cancel = vi.fn(async () => undefined);
    act(() => root.render(<RemotePaused b={BOUND} onCancel={cancel} />));
    expect(host.querySelector(".remote-paused")).toBeNull();
    act(() => root.render(<RemotePaused b={{ ...BOUND, connected: false, waiting: WAITING }} onCancel={cancel} />));
    expect(host.textContent).toContain("The turn is paused until laptop-graceful-unicorn reconnects");
    await act(async () => host.querySelector<HTMLButtonElement>("[data-action=cancel-turn]")!.click());
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  it("shows the request's code and posts it on Approve", async () => {
    vi.useFakeTimers({ now: NOW, toFake: ["Date"] });
    const post = vi.spyOn(api, "post").mockResolvedValue({ device: { id: "d1" } });
    const closed = vi.fn();
    act(() => root.render(<PairSheet code="wdjbmjht" request={REQUEST} loaded onClose={closed} />));
    const sheet = document.querySelector(".pair-sheet")!;
    expect(sheet.querySelector("[data-pair-code]")!.textContent).toBe("WDJB-MJHT");
    expect(sheet.textContent).toContain("laptop-graceful-unicorn");
    expect(sheet.textContent).toContain("macOS · ARM");
    expect(sheet.textContent).toContain("Expires in 10:00");
    await act(async () => sheet.querySelector<HTMLButtonElement>("[data-action=approve]")!.click());
    expect(post).toHaveBeenCalledWith("/api/cli/pair/WDJB-MJHT/approve", {});
    expect(closed).toHaveBeenCalled();
  });

  it("offers no buttons for a code nobody waits with, and refuses Approve once expired", () => {
    act(() => root.render(<PairSheet code="AAAA-BBBB" request={null} loaded onClose={() => undefined} />));
    expect(document.querySelector(".pair-sheet [data-action=approve]")).toBeNull();
    expect(document.querySelector(".pair-sheet")!.textContent).toContain("No terminal is waiting with this code");
    act(() => root.render(<PairSheet code="WDJBMJHT" request={{ ...REQUEST, expires_at: "2020-01-01T00:00:00Z" }} loaded onClose={() => undefined} />));
    expect(document.querySelector<HTMLButtonElement>(".pair-sheet [data-action=approve]")!.disabled).toBe(true);
  });

  it("says so when the request was answered elsewhere first", async () => {
    vi.useFakeTimers({ now: NOW, toFake: ["Date"] });
    vi.spyOn(api, "post").mockRejectedValue(new ApiError(409, "Request failed (409)"));
    const closed = vi.fn();
    act(() => root.render(<PairSheet code="WDJBMJHT" request={REQUEST} loaded onClose={closed} />));
    await act(async () => document.querySelector<HTMLButtonElement>(".pair-sheet [data-action=deny]")!.click());
    expect(document.querySelector(".pair-error")!.textContent).toBe("This request was already answered elsewhere.");
    expect(closed).not.toHaveBeenCalled();
  });

  it("asks in the row before revoking a device, and revokes only on the second press", async () => {
    // Paired a month ago, token rotated just now: the age is the token's, not the pairing's.
    const device: CliDevice = { id: "d1", name: "laptop-graceful-unicorn", hostname: "laptop", os: "linux", arch: "amd64", client_version: "0.1.0", paired_at: "2026-10-01T00:00:00Z",
      token_issued_at: new Date().toISOString(), approved_via: "app", last_seen_at: null, last_address: "127.0.0.1", expires_at: "2027-01-01T00:00:00Z", revoked_at: null, connected: true, sessions: 2 };
    prime("/api/cli/devices", { devices: [device] });
    vi.spyOn(api, "get").mockResolvedValue({ devices: [device] });
    const remove = vi.spyOn(api, "delete").mockResolvedValue({ revoked: true });
    act(() => root.render(<DevicesPanel toast={() => undefined} />));
    const row = host.querySelector("[data-device=d1]")!;
    expect(row.textContent).toContain("Connected");
    expect(row.textContent).toContain("2 chats");
    expect(row.innerHTML).toContain("issued today");
    act(() => row.querySelector<HTMLButtonElement>("[data-action=revoke]")!.click());
    expect(remove).not.toHaveBeenCalled();
    expect(row.textContent).toContain("Revoke laptop-graceful-unicorn?");
    await act(async () => row.querySelector<HTMLButtonElement>("[data-action=revoke-confirm]")!.click());
    expect(remove).toHaveBeenCalledWith("/api/cli/devices/d1");
  });
});
