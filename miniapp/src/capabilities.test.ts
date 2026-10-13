import { describe, expect, it } from "vitest";
import { changeNotice, screenTag, visibleScreens } from "./capabilities";
import type { Capabilities } from "./capabilities";
import type { Screen } from "./router";

// Written out rather than imported: router.ts reads window.history the moment it loads, and these
// are pure functions over a list of names.
const SCREENS: Screen[] = ["agents", "companion", "inbox", "board", "changes", "schedules", "services", "memory", "usage", "health", "settings"];
const BETA: Screen[] = ["harnesses"];

describe("visibleScreens", () => {
  it("drops Changes where the installation cannot change its own code", () => {
    expect(visibleScreens(SCREENS, "off")).not.toContain("changes");
    expect(visibleScreens(SCREENS, "off").length).toBe(SCREENS.length - 1);
  });
  it("keeps every destination in local and server mode", () => {
    expect(visibleScreens(SCREENS, "local")).toEqual(SCREENS);
    expect(visibleScreens(SCREENS, "server")).toEqual(SCREENS);
  });
  it("leaves a list that never had Changes alone", () => {
    const some: Screen[] = ["agents", "inbox", "settings"];
    expect(visibleScreens(some, "off")).toEqual(some);
  });
});

describe("screenTag", () => {
  it("marks Changes as local, because a change there stays on this machine", () => {
    expect(screenTag("changes", "local", BETA)).toBe("nav.tag.local");
    expect(screenTag("changes", "server", BETA)).toBe("");
  });
  it("still marks what is in beta", () => {
    expect(screenTag("harnesses", "server", BETA)).toBe("nav.tag.beta");
    expect(screenTag("inbox", "server", BETA)).toBe("");
  });
});

const base: Capabilities = { selfdev: { mode: "local", configured: "auto", reasons: [], missing: [], tools: [] } };
const pending = { repo: "bot", commit: "abc1234567", summary: "A quieter retry when the provider is busy" };

describe("changeNotice", () => {
  it("says nothing when nothing has changed", () => {
    expect(changeNotice(base, "")).toBeNull();
    expect(changeNotice(undefined, "")).toBeNull();
  });
  it("asks for the restart while a change is waiting, and offers the button", () => {
    const notice = changeNotice({ ...base, restart_required: pending }, "");
    expect(notice?.kind).toBe("pending");
    expect(notice?.action).toBe(true);
    expect(notice?.body).toContain("quieter retry");
  });
  it("warns that a restart cannot deliver a new image", () => {
    const notice = changeNotice({ ...base, restart_required: { ...pending, needs_image: true } }, "");
    expect(notice?.body).toContain("rewrites the image");
  });
  it("prefers the change that is waiting over the one already decided", () => {
    const caps = { ...base, restart_required: pending, last_change: { ...pending, commit: "old", status: "applied" } };
    expect(changeNotice(caps, "")?.commit).toBe(pending.commit);
  });
  it("reports how the last change ended, once, until it is dismissed", () => {
    const caps = { ...base, last_change: { ...pending, status: "applied" } };
    expect(changeNotice(caps, "")?.kind).toBe("done");
    expect(changeNotice(caps, pending.commit)).toBeNull();
  });
  it("gives the supervisor's own reason when the change was refused or reversed", () => {
    const refused = { ...base, last_change: { ...pending, status: "preflight_failed", detail: "the checks did not pass" } };
    expect(changeNotice(refused, "")).toMatchObject({ kind: "failed", title: "The change was not applied", body: "the checks did not pass" });
    const reversed = { ...base, last_change: { ...pending, status: "rolled_back", detail: "three starts died" } };
    expect(changeNotice(reversed, "")?.title).toBe("The change was reversed");
  });
});
