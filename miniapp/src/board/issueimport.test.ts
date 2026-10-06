import { describe, expect, it } from "vitest";
import { IssueAction, IssueEntry, IssueListing, counts, keepPicked, normalizeRepository, selectable, selectionItems, toggleAll, validRepository } from "./issueimport";

function entry(number: number, action: IssueAction, fields: Partial<IssueEntry> = {}): IssueEntry {
  const linked = action !== "import" && action !== "too_large" && action !== "elsewhere";
  return {
    number,
    title: `issue ${number}`,
    url: `https://github.com/owner/repo/issues/${number}`,
    labels: [],
    body_length: 10,
    action,
    task_id: linked ? `t${number}` : null,
    task_title: linked ? `task ${number}` : null,
    preview_digest: action === "elsewhere" ? null : "d".repeat(64),
    expected_entity_revision: linked ? 3 : null,
    ...fields,
  };
}

function listing(...issues: IssueEntry[]): IssueListing {
  return { project_id: "p", repository: "owner/repo", label: null, collection_revision: 4, limit: 100, issues };
}

describe("issue import", () => {
  it("reads a pasted GitHub address as owner/repo and refuses anything else", () => {
    expect(normalizeRepository(" https://github.com/owner/repo.git ")).toBe("owner/repo");
    expect(normalizeRepository("git@github.com:owner/repo")).toBe("owner/repo");
    expect(validRepository("owner/repo")).toBe(true);
    expect(validRepository("owner")).toBe(false);
    expect(validRepository("owner/../x")).toBe(false);
    expect(validRepository("owner/re po")).toBe(false);
  });

  it("lets only new and changed issues be ticked", () => {
    expect(selectable(entry(1, "import"))).toBe(true);
    expect(selectable(entry(2, "update"))).toBe(true);
    for (const action of ["linked", "conflict", "too_large", "elsewhere"] as IssueAction[]) expect(selectable(entry(3, action))).toBe(false);
  });

  it("sends the ticked entries in listing order with their digests and card revisions", () => {
    const list = listing(entry(9, "import"), entry(8, "linked"), entry(7, "update"));
    const items = selectionItems(list, new Set([7, 8, 9]));
    expect(items).toEqual([
      { issue_number: 9, action: "import", preview_digest: "d".repeat(64), expected_entity_revision: null },
      { issue_number: 7, action: "update", preview_digest: "d".repeat(64), expected_entity_revision: 3 },
    ]);
  });

  it("ticks every selectable entry, then clears them", () => {
    const list = listing(entry(1, "import"), entry(2, "too_large"), entry(3, "update"));
    const all = toggleAll(list, new Set());
    expect([...all].sort()).toEqual([1, 3]);
    expect(toggleAll(list, all).size).toBe(0);
  });

  it("keeps only ticks that are still selectable after a new listing", () => {
    const next = listing(entry(1, "linked"), entry(2, "import"));
    expect([...keepPicked(next, new Set([1, 2, 5]))]).toEqual([2]);
  });

  it("counts entries by what they offer", () => {
    const tally = counts(listing(entry(1, "import"), entry(2, "import"), entry(3, "conflict")));
    expect(tally.import).toBe(2);
    expect(tally.conflict).toBe(1);
    expect(tally.update).toBe(0);
  });
});
