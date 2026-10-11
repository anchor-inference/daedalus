// @vitest-environment jsdom
// The settings-change card under an answer: it is drawn from the id the tool's answer names, shows
// each change from old to new with when it takes effect, offers Apply and Reject only while the change
// waits, and takes the host's word for how it ended — including an answer given elsewhere first.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./api";
import { setLang, t } from "./i18n";
import { applyAt, SettingsChangeCard, settingsChangeIds, SettingsChangeView, shownValue } from "./settingschange";
import type { Activity } from "./turns";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const pending: SettingsChangeView = {
  id: "sc0123456789",
  session_id: "s1",
  reason: "The operator wants longer runs for the migration.",
  changes: [
    { path: "limits.max_iterations", old: 200, new: 400, apply_at: "next_step", kind: "approval" },
    { path: "telegram.reactions", old: true, new: false, apply_at: "restart", kind: "approval" },
  ],
  kind: "approval",
  status: "pending",
  created_at: "2026-10-11T10:00:00+00:00",
  expires_at: "2026-10-12T10:00:00+00:00",
  decided_at: null,
  via: "",
  error: "",
};

describe("the settings change a turn made", () => {
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
  });

  const button = (action: string) => host.querySelector<HTMLButtonElement>(`button[data-action="${action}"]`);

  it("reads the change's id off the step that answered with one, and only off that", () => {
    const activity: Activity[] = [
      { kind: "tool", id: "a", name: "SettingsGet", args: {}, running: false, result: "Settings change: sc9999999999" },
      { kind: "tool", id: "b", name: "SettingsChange", args: {}, running: false, result: "Settings change: sc0123456789\nWaiting for the operator" },
      { kind: "tool", id: "c", name: "SettingsChange", args: {}, running: false, error: true, result: "the change is not valid" },
      { kind: "tool", id: "d", name: "SettingsChange", args: {}, running: true },
      { kind: "tool", id: "e", name: "SettingsChange", args: {}, running: false, result: "Settings change: sc0123456789\nWaiting" },
    ];
    expect(settingsChangeIds(activity)).toEqual(["sc0123456789"]);
  });

  it("says values and the moment it takes effect in words", () => {
    expect(shownValue(null)).toBe("not set");
    expect(shownValue(true)).toBe("on");
    expect(shownValue("")).toBe("empty");
    expect(shownValue(["a", "b"])).toBe('["a","b"]');
    expect(applyAt(pending.changes)).toBe(t("setchange.at.restart"));
    expect(applyAt([pending.changes[0]])).toBe(t("setchange.at.next_step"));
  });

  it("shows every change from old to new and applies it on Apply", async () => {
    const get = vi.spyOn(api, "get").mockResolvedValue(pending);
    const post = vi.spyOn(api, "post").mockResolvedValue({ ...pending, status: "applied", decided_at: "2026-10-11T10:01:00+00:00", via: "app" });
    await act(async () => root.render(<SettingsChangeCard changeId="sc0123456789" />));
    expect(get).toHaveBeenCalledWith("/api/settings/changes/sc0123456789");
    const card = host.querySelector(".setchange")!;
    expect(card.getAttribute("data-status")).toBe("pending");
    expect(card.textContent).toContain("Waiting for you");
    expect(card.textContent).toContain("The operator wants longer runs");
    const lines = [...host.querySelectorAll(".setchange-line")].map((li) => li.textContent);
    expect(lines).toEqual(["limits.max_iterations200→400", "telegram.reactionson→off"]);
    expect(card.textContent).toContain("Takes effect after a restart once applied");
    await act(async () => button("apply")!.click());
    expect(post).toHaveBeenCalledWith("/api/settings/changes/sc0123456789/apply");
    expect(host.querySelector(".setchange")!.getAttribute("data-status")).toBe("applied");
    expect(host.textContent).toContain("Applied");
    expect(button("apply")).toBeNull();
    expect(button("reject")).toBeNull();
  });

  it("shows the answer given elsewhere first instead of an error", async () => {
    vi.spyOn(api, "get").mockResolvedValue(pending);
    vi.spyOn(api, "post").mockRejectedValue(new ApiError(409, "Request failed (409)", { ...pending, status: "rejected", via: "notification" }));
    await act(async () => root.render(<SettingsChangeCard changeId="sc0123456789" />));
    await act(async () => button("apply")!.click());
    expect(host.querySelector(".setchange")!.getAttribute("data-status")).toBe("rejected");
    expect(host.textContent).toContain("Nothing was changed");
    expect(host.querySelector(".setchange-note.error")).toBeNull();
  });

  it("draws a change applied at once, or one that went stale, without buttons", async () => {
    vi.spyOn(api, "get").mockResolvedValue({ ...pending, kind: "auto", status: "applied" });
    await act(async () => root.render(<SettingsChangeCard changeId="sc0123456789" />));
    expect(host.textContent).toContain("Settings changed");
    expect(button("apply")).toBeNull();
    vi.spyOn(api, "get").mockResolvedValue({ ...pending, status: "stale" });
    await act(async () => root.render(<SettingsChangeCard key="again" changeId="sc0123456789" />));
    expect(host.textContent).toContain("Outdated");
    expect(host.textContent).toContain("propose it again");
  });

  it("says a change the host no longer keeps is gone", async () => {
    vi.spyOn(api, "get").mockRejectedValue(new ApiError(404, "no such settings change"));
    await act(async () => root.render(<SettingsChangeCard changeId="sc0123456789" />));
    expect(host.textContent).toContain("no longer kept");
  });
});
