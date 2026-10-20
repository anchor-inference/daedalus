import { describe, expect, it } from "vitest";
import type { WorkerWorktree } from "./ProjectWorktrees";
import { worktreeFolders } from "./worktreefolders";
import { placeOf } from "../turnfiles";

const row = (extra: Partial<WorkerWorktree>): WorkerWorktree => ({
  folder_id: "f1", path: "/work/site/.agents/worktrees/hrdrafter", name: "hrdrafter", member: { id: "m1", name: "hrdrafter", dismissed: false },
  branch: "agent/hrdrafter/t1-act", base: "main", size_bytes: 10, changes: 1, last_commit_at: "", merged: false, prunable: false,
  live: null, blocked: "", pane: "wt~f1~hrdrafter", ...extra,
});

describe("the staff worktrees in the orchestrator's file pane", () => {
  it("makes one read-only folder per worktree the host can serve, in the environment of its project folder", () => {
    const listing = { worktrees: [row({}), row({ name: "gone", prunable: true, pane: "wt~f1~gone" }), row({ name: "far", pane: "" }), row({ name: "lone", member: null, branch: "", pane: "wt~f1~lone" })], problems: [] };
    expect(worktreeFolders(listing, [{ id: "f1", env: "host" }])).toEqual([
      { id: "wt~f1~hrdrafter", path: "/work/site/.agents/worktrees/hrdrafter", label: "hrdrafter · agent/hrdrafter/t1-act", env: "host", readonly: true, writable: false },
      { id: "wt~f1~lone", path: "/work/site/.agents/worktrees/hrdrafter", label: "lone", env: "host", readonly: true, writable: false },
    ]);
    expect(worktreeFolders(undefined, [])).toEqual([]);
  });

  it("places a file a member wrote in the member's worktree, not in the project folder around it", () => {
    const folders = [{ id: "f1", path: "/work/site" }, { id: "wt~f1~hrdrafter", path: "/work/site/.agents/worktrees/hrdrafter" }];
    const path = "/work/site/.agents/worktrees/hrdrafter/docs/act.docx";
    expect(placeOf(path, "/work/site", folders)).toEqual({ rel: "docs/act.docx", folder: "wt~f1~hrdrafter" });
    expect(placeOf("/work/site/README.md", "/work/site", folders)).toEqual({ rel: "README.md", folder: "" });
  });
});
