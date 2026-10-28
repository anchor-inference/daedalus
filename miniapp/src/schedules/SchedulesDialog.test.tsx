// @vitest-environment jsdom
// The schedules dialog a chat's schedule card opens: that chat's schedules with the clicked one open,
// a switch to everyone's, a filter, Escape to close, and the commands answering the host's routes
// with the revision shown and an approval as long as the host names.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { setLang } from "../i18n";
import { invalidate } from "../store";
import type { ProposalList, ScheduleRow } from "./model";
import { SchedulesDialog } from "./SchedulesDialog";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const soon = new Date(Date.now() + 2 * 3600000).toISOString();
const schedule = (over: Partial<ScheduleRow>): ScheduleRow => ({
  id: "s-mine", name: "Mine hourly check", cron: "5 * * * *", run_at: null, prompt: "Check the mine.\n\n" + "Read the logs. ".repeat(60),
  enabled: 1, next_run_at: soon, last_run_at: null, last_summary: null, kind: "agent", target_session: "chat-a", run_in: "self",
  created_by_session: "chat-a", active_session_id: null, failure_count: 0, last_error: null, authority_state: "current",
  schedule_revision: 4, project_id: null, ...over,
});
const SCHEDULES = [
  schedule({}),
  schedule({ id: "s-backup", name: "Backup check", cron: "30 3 * * *", target_session: null, run_in: "new", created_by_session: "chat-b", prompt: "Verify the backup" }),
];
const PROPOSALS: ProposalList = {
  entries: [{ id: "p-reset", name: "Mine reset", prompt: "Reset", cron: "10 2 * * *", run_at: null, kind: "agent", next_run_at: soon, file_count: 0, files: [],
    legacy_file_review_required: false, source_session_id: "chat-a", source_project_id: null, proposal_revision: 1, request_digest: "digest", status: "pending", created_at: soon }],
  collection_revisions: { "global:global": 9 },
  require_approval: false,
  approval_days: 3650,
};

function answer(path: string): unknown {
  if (path === "/api/schedules") return SCHEDULES;
  if (path === "/api/recurring/proposals") return PROPOSALS;
  if (path === "/api/sessions") return { sessions: [{ id: "chat-a", title: "Minecraft server" }, { id: "chat-b", title: "Ops" }], projects: [] };
  if (path.endsWith("/cycles")) return { schedule_revision: 4, authority_state: "current", collection_revision: 9, scope: { kind: "global", id: "global" }, cycles: [] };
  throw new Error(`unexpected ${path}`);
}

describe("the schedules dialog over a chat", () => {
  let host: HTMLDivElement;
  let root: Root;
  let closed: number;
  beforeEach(() => {
    setLang("en");
    closed = 0;
    for (const key of ["/api/schedules", "/api/recurring", "/api/sessions"]) invalidate(key);
    // A desktop window: the list and the schedule side by side.
    window.matchMedia = ((query: string) => ({ matches: query.includes("min-width: 1024px"), addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia;
    vi.spyOn(api, "get").mockImplementation(async (path: string) => answer(path) as never);
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    vi.restoreAllMocks();
  });

  const dialog = () => document.querySelector<HTMLElement>(".schedules-dialog")!;
  const rows = () => [...document.querySelectorAll<HTMLElement>(".schedrow")].map((el) => el.dataset.schedule);
  const button = (name: string) => [...dialog().querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === name || b.getAttribute("aria-label") === name)!;

  async function open(selected: string | null) {
    await act(async () => root.render(<SchedulesDialog sessionId="chat-a" selected={selected} onClose={() => (closed += 1)} onOpenSession={() => undefined} />));
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }

  it("opens on this chat's schedules with the clicked one open, and the switch shows everyone's", async () => {
    await open("s-mine");
    expect(dialog().getAttribute("role")).toBe("dialog");
    expect(rows()).toEqual(["p-reset", "s-mine"]);
    const detail = dialog().querySelector(".scheddetail")!;
    expect(detail.getAttribute("data-schedule")).toBe("s-mine");
    expect(detail.textContent).toContain("Every hour at :");
    // The long instruction is folded with a way to read it whole.
    expect(detail.querySelector(".scheddetail-prompt.folded")).not.toBeNull();
    await act(async () => button("All").click());
    expect(rows()).toEqual(["p-reset", "s-mine", "s-backup"]);
    const search = dialog().querySelector<HTMLInputElement>("input[type=search]")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(search, "backup");
      search.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(rows()).toEqual(["s-backup"]);
  });

  it("closes on Escape", async () => {
    await open(null);
    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(closed).toBe(1);
  });

  it("pauses with the revision shown, and an Accept by hand approves for the host's ten years", async () => {
    const patch = vi.spyOn(api, "patch").mockResolvedValue({ schedule_revision: 5, entity_revision: 10 } as never);
    const post = vi.spyOn(api, "post").mockResolvedValue({ id: "s-reset" } as never);
    await open("s-mine");
    await act(async () => button("Pause").click());
    const [path, body] = patch.mock.calls[0] as [string, Record<string, unknown>];
    expect(path).toBe("/api/recurring/s-mine");
    expect(body).toMatchObject({ enabled: false, expected_schedule_revision: 4, expected_collection_revision: 9 });
    expect(post).not.toHaveBeenCalled();

    const row = document.querySelector<HTMLElement>('.schedrow[data-schedule="p-reset"]')!;
    const accept = [...row.querySelectorAll("button")].find((b) => b.textContent === "Approve schedule")!;
    await act(async () => accept.click());
    const [acceptPath, acceptBody] = post.mock.calls[0] as [string, Record<string, unknown>];
    expect(acceptPath).toBe("/api/recurring/proposals/p-reset/accept");
    expect(acceptBody).toMatchObject({ request_digest: "digest", expected_proposal_revision: 1, expected_collection_revision: 9 });
    expect(Date.parse(String(acceptBody.expires_at))).toBeGreaterThan(Date.now() + 3000 * 86400000);
  });

  it("resumes and approves again, since a resumed schedule keeps no approval of its own", async () => {
    SCHEDULES[0] = schedule({ enabled: 0 });
    const patch = vi.spyOn(api, "patch").mockResolvedValue({ schedule_revision: 5, entity_revision: 10 } as never);
    const post = vi.spyOn(api, "post").mockResolvedValue({} as never);
    try {
      await open("s-mine");
      await act(async () => button("Resume").click());
      expect((patch.mock.calls[0][1] as Record<string, unknown>).enabled).toBe(true);
      const [path, body] = post.mock.calls[0] as [string, Record<string, unknown>];
      expect(path).toBe("/api/recurring/s-mine/approve");
      expect(body).toMatchObject({ expected_schedule_revision: 5, expected_collection_revision: 10 });
    } finally {
      SCHEDULES[0] = schedule({});
    }
  });
});
